import binascii
from concurrent.futures import ThreadPoolExecutor
from homeassistant import core
from homeassistant.const import (
	CONF_PASSWORD,
	EVENT_HOMEASSISTANT_STOP,
	STATE_ALARM_DISARMED,
	STATE_ALARM_ARMED_AWAY,
	STATE_ALARM_ARMED_NIGHT,
	STATE_ALARM_ARMING,
	STATE_ALARM_PENDING,
	STATE_ALARM_TRIGGERED,
	STATE_OFF,
	STATE_ON,
)
from homeassistant.helpers.entity import Entity
import re
import sys
import threading
import time
from typing import Any, Dict, List, Optional
from .const import (
	CONF_DEVICES,
	CONF_NUMBER_OF_DEVICES,
	CONF_SERIAL_PORT,
	CONF_REQUIRE_CODE_TO_ARM,
	CONF_REQUIRE_CODE_TO_DISARM,
	DEFAULT_CONF_REQUIRE_CODE_TO_ARM,
	DEFAULT_CONF_REQUIRE_CODE_TO_DISARM,
	DEVICES,
	DEVICE_KEYPAD,
	DEVICE_SIREN,
	DEVICE_OTHER,
	DOMAIN,
	LOGGER,
	MAX_SECTIONS,
)
from .errors import (
	ModelNotDetected,
	ModelNotSupported,
	ServiceUnavailable,
	ShouldNotHappen,
)

MAX_WORKERS = 5
TIMEOUT = 10
PACKET_READ_SIZE = 64

# x02 model
# x08 hardware version
# x09 firmware version
# x0a registration code
# x0b name of the installation
JABLOTRON_PACKET_GET_MODEL = b"\x30\x01\x02"
JABLOTRON_PACKET_GET_INFO = b"\x30\x01\x02\x30\x01\x03\x30\x01\x04\x30\x01\x05\x30\x01\x06\x30\x01\x07\x30\x01\x08\x30\x01\x09"
JABLOTRON_PACKET_GET_SECTIONS_STATES = b"\x80\x01\x01\x52\x01\x0e"
JABLOTRON_PACKET_SECTIONS_STATES_PREFIX = b"\x51\x22"
JABLOTRON_PACKET_DEVICES_STATES_PREFIX = b"\xd8"
JABLOTRON_PACKET_WIRED_DEVICE_STATE_PREFIX = b"\x55\x08"
JABLOTRON_PACKET_WIRELESS_DEVICE_STATE_PREFIX = b"\x55\x09"
JABLOTRON_PACKET_INFO_PREFIX = b"\x40"
JABLOTRON_INFO_MODEL = b"\x02"
JABLOTRON_INFO_HARDWARE_VERSION = b"\x08"
JABLOTRON_INFO_FIRMWARE_VERSION = b"\x09"
JABLOTRON_INFO_REGISTRATION_CODE = b"\x0a"
JABLOTRON_INFO_INSTALLATION_NAME = b"\x0b"

JABLOTRON_PRIMARY_STATE_DISARMED = 1
JABLOTRON_PRIMARY_STATE_ARMED_PARTIALLY = 2
JABLOTRON_PRIMARY_STATE_ARMED_FULL = 3

JABLOTRON_SECONDARY_STATE_OK = 0
JABLOTRON_SECONDARY_STATE_TRIGGERED = 1
JABLOTRON_SECONDARY_STATE_PROBLEM = 2
JABLOTRON_SECONDARY_STATE_PENDING = 4
JABLOTRON_SECONDARY_STATE_ARMING = 8

JABLOTRON_TERTIARY_STATE_OFF = 0
JABLOTRON_TERTIARY_STATE_ON = 1

def decode_info_bytes(value: bytes) -> str:
	info = ""

	for i in range(len(value)):
		letter = value[i:(i + 1)]

		if letter == b"\x00":
			break

		info += letter.decode()

	return info

def check_serial_port(serial_port: str) -> None:
	stop_event = threading.Event()
	thread_pool_executor = ThreadPoolExecutor(max_workers=MAX_WORKERS)

	def reader_thread() -> Optional[str]:
		model = None

		stream = open(serial_port, "rb")

		try:
			while not stop_event.is_set():
				packet = stream.read(PACKET_READ_SIZE)
				LOGGER.debug(str(binascii.hexlify(packet), "utf-8"))

				if packet[:1] == JABLOTRON_PACKET_INFO_PREFIX and packet[2:3] == JABLOTRON_INFO_MODEL:
					try:
						model = decode_info_bytes(packet[3:])
						break
					except UnicodeDecodeError:
						# Try again
						pass
		finally:
			stream.close()

		return model

	def writer_thread() -> None:
		while not stop_event.is_set():
			stream = open(serial_port, "wb")

			stream.write(JABLOTRON_PACKET_GET_MODEL)
			time.sleep(0.1)

			stream.close()

			time.sleep(1)

	try:
		reader = thread_pool_executor.submit(reader_thread)
		thread_pool_executor.submit(writer_thread)

		model = reader.result(TIMEOUT)

		if model is None:
			raise ModelNotDetected

		if not re.match(r"JA-10[1367]", model):
			raise ModelNotSupported("Model {} not supported".format(model))

	except (IndexError, FileNotFoundError, IsADirectoryError, UnboundLocalError, OSError):
		raise ServiceUnavailable

	finally:
		stop_event.set()
		thread_pool_executor.shutdown()


class JablotronCentralUnit:

	def __init__(self, serial_port: str, model: str, hardware_version: str, firmware_version: str):
		self.serial_port: str = serial_port
		self.model: str = model
		self.hardware_version: str = hardware_version
		self.firmware_version: str = firmware_version


class JablotronControl:

	def __init__(self, central_unit: JablotronCentralUnit, name: str, id: str, friendly_name: Optional[str] = None):
		self.central_unit: JablotronCentralUnit = central_unit
		self.name: str = name
		self.id: str = id
		self.friendly_name: Optional[str] = friendly_name


class JablotronDevice(JablotronControl):

	def __init__(self, central_unit: JablotronCentralUnit, name: str, id: str, type: str):
		self.type: str = type

		super().__init__(central_unit, name, id)


class JablotronAlarmControlPanel(JablotronControl):

	def __init__(self, central_unit: JablotronCentralUnit, section: int, name: str, id: str):
		self.section: int = section

		super().__init__(central_unit, name, id)


class Jablotron():

	def __init__(self, hass: core.HomeAssistant, config: Dict[str, Any], options: Dict[str, Any]) -> None:
		self._hass: core.HomeAssistant = hass
		self._config: Dict[str, Any] = config
		self._options: Dict[str, Any] = options

		self._central_unit: Optional[JablotronCentralUnit] = None
		self._alarm_control_panels: List[JablotronAlarmControlPanel] = []
		self._section_problem_sensors: List[JablotronControl] = []
		self._device_sensors: List[JablotronDevice] = []
		self._device_problem_sensors: List[JablotronControl] = []

		self._entities: Dict[str, JablotronEntity] = {}

		self._state_checker_thread_pool_executor: Optional[ThreadPoolExecutor] = None
		self._state_checker_stop_event: threading.Event = threading.Event()
		self._state_checker_data_updating_event: threading.Event = threading.Event()

		self.states: Dict[str, str] = {}
		self.last_update_success: bool = False

	def update_options(self, options: Dict[str, Any]) -> None:
		self._options = options
		self._update_all_entities()

	def is_code_required_for_disarm(self) -> bool:
		return self._options.get(CONF_REQUIRE_CODE_TO_DISARM, DEFAULT_CONF_REQUIRE_CODE_TO_DISARM)

	def is_code_required_for_arm(self) -> bool:
		return self._options.get(CONF_REQUIRE_CODE_TO_ARM, DEFAULT_CONF_REQUIRE_CODE_TO_ARM)

	def initialize(self) -> None:
		def shutdown_event(_):
			self.shutdown()

		self._hass.bus.async_listen(EVENT_HOMEASSISTANT_STOP, shutdown_event)

		self._detect_central_unit()
		self._detect_sections()
		self._create_devices()

		# Initialize states checker
		self._state_checker_thread_pool_executor = ThreadPoolExecutor(max_workers=MAX_WORKERS)
		self._state_checker_thread_pool_executor.submit(self._read_packets)
		self._state_checker_thread_pool_executor.submit(self._keepalive)

		self.last_update_success = True

	def central_unit(self) -> JablotronCentralUnit:
		return self._central_unit

	def shutdown(self) -> None:
		self._state_checker_stop_event.set()

		# Send packet so read thread can finish
		self._send_packet(JABLOTRON_PACKET_GET_SECTIONS_STATES)

		if self._state_checker_thread_pool_executor is not None:
			self._state_checker_thread_pool_executor.shutdown()

	def substribe_entity_for_updates(self, control_id: str, entity) -> None:
		self._entities[control_id] = entity

	def modify_alarm_control_panel_section_state(self, section: int, state: str, code: Optional[str]) -> None:
		if code is None:
			code = self._config[CONF_PASSWORD]

		int_packets = {
			STATE_ALARM_DISARMED: 143,
			STATE_ALARM_ARMED_AWAY: 159,
			STATE_ALARM_ARMED_NIGHT: 175,
		}

		state_packet = Jablotron._int_to_bytes(int_packets[state] + section)

		self._send_packet(self._create_code_packet(code) + b"\x80\x02\x0d" + state_packet)

	def alarm_control_panels(self) -> List[JablotronAlarmControlPanel]:
		return self._alarm_control_panels

	def section_problem_sensors(self) -> List[JablotronControl]:
		return self._section_problem_sensors

	def device_sensors(self) -> List[JablotronDevice]:
		return self._device_sensors

	def device_problem_sensors(self) -> List[JablotronControl]:
		return self._device_problem_sensors

	def _update_all_entities(self) -> None:
		for entity in self._entities.values():
			entity.async_write_ha_state()

	def _detect_central_unit(self) -> None:
		stop_event = threading.Event()
		thread_pool_executor = ThreadPoolExecutor(max_workers=MAX_WORKERS)

		def reader_thread() -> Optional[JablotronCentralUnit]:
			model = None
			hardware_version = None
			firmware_version = None

			stream = open(self._config[CONF_SERIAL_PORT], "rb")

			try:
				while not stop_event.is_set():
					packet = stream.read(PACKET_READ_SIZE)
					LOGGER.debug(str(binascii.hexlify(packet), "utf-8"))

					if packet[:1] == JABLOTRON_PACKET_INFO_PREFIX:
						try:
							if packet[2:3] == JABLOTRON_INFO_MODEL:
								model = decode_info_bytes(packet[3:])
							elif packet[2:3] == JABLOTRON_INFO_HARDWARE_VERSION:
								hardware_version = decode_info_bytes(packet[3:])
							elif packet[2:3] == JABLOTRON_INFO_FIRMWARE_VERSION:
								firmware_version = decode_info_bytes(packet[3:])
						except UnicodeDecodeError:
							# Try again
							pass

					if model is not None and hardware_version is not None and firmware_version is not None:
						break
			finally:
				stream.close()

			if model is None or hardware_version is None or firmware_version is None:
				return None

			return JablotronCentralUnit(self._config[CONF_SERIAL_PORT], model, hardware_version, firmware_version)

		def writer_thread() -> None:
			while not stop_event.is_set():
				self._send_packet(JABLOTRON_PACKET_GET_INFO)
				time.sleep(1)

		try:
			reader = thread_pool_executor.submit(reader_thread)
			thread_pool_executor.submit(writer_thread)

			self._central_unit = reader.result(TIMEOUT)

		except (IndexError, FileNotFoundError, IsADirectoryError, UnboundLocalError, OSError) as ex:
			LOGGER.error(format(ex))
			raise ServiceUnavailable

		finally:
			stop_event.set()
			thread_pool_executor.shutdown()

		if self._central_unit is None:
			raise ShouldNotHappen

	def _detect_sections(self) -> None:
		stop_event = threading.Event()
		thread_pool_executor = ThreadPoolExecutor(max_workers=MAX_WORKERS)

		def reader_thread() -> Optional[Dict[int, bytes]]:
			section_states = None

			stream = open(self._config[CONF_SERIAL_PORT], "rb")

			try:
				while not stop_event.is_set():
					packet = stream.read(PACKET_READ_SIZE)

					if packet[:2] == JABLOTRON_PACKET_SECTIONS_STATES_PREFIX:
						section_states = Jablotron._parse_sections_states_packet(packet)
						break
			finally:
				stream.close()

			if section_states is None:
				return None

			return section_states

		def writer_thread() -> None:
			while not stop_event.is_set():
				self._send_packet(JABLOTRON_PACKET_GET_SECTIONS_STATES)
				time.sleep(1)

		try:
			reader = thread_pool_executor.submit(reader_thread)
			thread_pool_executor.submit(writer_thread)

			section_states = reader.result(TIMEOUT)

		except (IndexError, FileNotFoundError, IsADirectoryError, UnboundLocalError, OSError) as ex:
			LOGGER.error(format(ex))
			raise ServiceUnavailable

		finally:
			stop_event.set()
			thread_pool_executor.shutdown()

		if section_states is None:
			raise ShouldNotHappen

		for section, section_state in section_states.items():
			section_alarm_id = Jablotron._create_section_alarm_id(section)
			section_problem_sensor_id = Jablotron._create_section_problem_sensor_id(section)

			self._alarm_control_panels.append(JablotronAlarmControlPanel(
				self._central_unit,
				section,
				self._create_section_name(section),
				section_alarm_id,
			))
			self._section_problem_sensors.append(JablotronControl(
				self._central_unit,
				self._create_section_problem_sensor_name(section),
				section_problem_sensor_id,
			))

			self.states[section_alarm_id] = Jablotron._convert_jablotron_alarm_state_to_alarm_state(section_state)
			self.states[section_problem_sensor_id] = Jablotron._convert_jablotron_alarm_state_to_problem_sensor_state(section_state)

	def _create_devices(self) -> None:
		for i in range(self._config[CONF_NUMBER_OF_DEVICES]):
			type = self._config[CONF_DEVICES][i]

			if (
					type == DEVICE_KEYPAD
					or type == DEVICE_SIREN
					or type == DEVICE_OTHER
			):
				continue

			number = i + 1

			device_name = Jablotron._create_device_sensor_name(type, number)
			device_id = Jablotron._create_device_sensor_id(number)
			device_problem_sensor_id = Jablotron._create_device_problem_sensor_id(number)

			self._device_sensors.append(JablotronDevice(
				self._central_unit,
				device_name,
				device_id,
				type,
			))
			self._device_problem_sensors.append(JablotronControl(
				self._central_unit,
				device_name,
				device_problem_sensor_id,
				Jablotron._create_device_problem_sensor_name(type, number),
			))

			self.states[device_id] = STATE_OFF
			self.states[device_problem_sensor_id] = STATE_OFF

	def _read_packets(self) -> None:
		stream = open(self._config[CONF_SERIAL_PORT], "rb")

		while not self._state_checker_stop_event.is_set():

			try:

				while True:

					self._state_checker_data_updating_event.clear()

					packet = stream.read(PACKET_READ_SIZE)
					# LOGGER.debug(str(binascii.hexlify(packet), "utf-8"))

					self._state_checker_data_updating_event.set()

					if not packet:
						self.last_update_success = False
						self._update_all_entities()
						break

					self.last_update_success = True

					prefix = packet[:2]

					if prefix == JABLOTRON_PACKET_SECTIONS_STATES_PREFIX:
						self._parse_section_states_packet(packet)
						break

					if (Jablotron._is_device_state_packet(prefix)):
						self._parse_device_state_packet(packet)
						break

					if packet[:1] == JABLOTRON_PACKET_DEVICES_STATES_PREFIX:
						self._parse_devices_states_packet(packet)
						break

			except Exception as ex:
				LOGGER.error("Read error: {}".format(format(ex)))
				self.last_update_success = False
				self._update_all_entities()

			time.sleep(0.5)

		stream.close()

	def _keepalive(self):
		counter = 0
		while not self._state_checker_stop_event.is_set():
			if not self._state_checker_data_updating_event.wait(0.5):
				try:
					if counter == 0:
						self._send_packet(self._create_code_packet(self._config[CONF_PASSWORD]) + b"\x52\x02\x13\x05\x9a")
					else:
						self._send_packet(b"\x52\x01\x02")
				except Exception as ex:
					LOGGER.error("Write error: {}".format(format(ex)))

			time.sleep(1)
			counter += 1
			if counter == 60:
				counter = 0

	def _send_packet(self, packet) -> None:
		stream = open(self._config[CONF_SERIAL_PORT], "wb")

		stream.write(packet)
		time.sleep(0.1)

		stream.close()

	def _update_state(self, id: str, state: str) -> None:
		if id in self.states and state == self.states[id]:
			return

		self.states[id] = state

		if id in self._entities:
			self._entities[id].async_write_ha_state()

	def _parse_section_states_packet(self, packet: bytes) -> None:
		section_states = Jablotron._parse_sections_states_packet(packet)

		for section, section_state in section_states.items():
			self._update_state(
				Jablotron._create_section_alarm_id(section),
				Jablotron._convert_jablotron_alarm_state_to_alarm_state(section_state),
			)

			self._update_state(
				Jablotron._create_section_problem_sensor_id(section),
				Jablotron._convert_jablotron_alarm_state_to_problem_sensor_state(section_state),
			)

	def _parse_device_state_packet(self, packet: bytes) -> None:
		device_number = Jablotron._parse_device_number_from_state_packet(packet)
		device_state = Jablotron._convert_jablotron_device_state_to_state(packet, device_number)
		device_problem_sensor_state = Jablotron._convert_jablotron_device_state_to_problem_sensor_state(packet)

		self._update_state(
			Jablotron._create_device_problem_sensor_id(device_number),
			device_problem_sensor_state,
		)

		if device_state is not None:
			self._update_state(
				Jablotron._create_device_sensor_id(device_number),
				device_state,
			)
		else:
			LOGGER.error("Unknown device state packet: {}".format(str(binascii.hexlify(packet), "utf-8")))

	def _parse_devices_states_packet(self, packet: bytes) -> None:
		states_start_packet = 3
		triggered_device_start_packet = states_start_packet + Jablotron._bytes_to_int(packet[1:2]) - 1

		states = Jablotron._hex_to_bin(packet[states_start_packet:triggered_device_start_packet])

		if Jablotron._is_device_state_packet(packet[triggered_device_start_packet:(triggered_device_start_packet + 2)]):
			self._parse_device_state_packet(packet[triggered_device_start_packet:])

		for i in range(1, self._config[CONF_NUMBER_OF_DEVICES] + 1):
			device_state = STATE_ON if states[i:(i + 1)] == "1" else STATE_OFF
			self._update_state(
				Jablotron._create_device_sensor_id(i),
				device_state,
			)

	def _create_code_packet(self, code: str) -> bytes:
		code_packet = b"\x80\x08\x03\x39\x39\x39" if self._is_small_central_unit() else b"\x80\x08\x03\x30"

		for code_number in code:
			code_packet += Jablotron._int_to_bytes(48 + int(code_number))

		return code_packet

	def _is_small_central_unit(self) -> bool:
		return re.match(r"JA-10[13]", self._central_unit.model) is not None

	@staticmethod
	def _is_device_state_packet(prefix) -> bool:
		return prefix == JABLOTRON_PACKET_WIRED_DEVICE_STATE_PREFIX or prefix == JABLOTRON_PACKET_WIRELESS_DEVICE_STATE_PREFIX

	@staticmethod
	def _parse_sections_states_packet(packet: bytes) -> Dict[int, bytes]:
		section_states = {}

		for section in range(1, MAX_SECTIONS + 1):
			state_offset = section * 2
			state = packet[state_offset:(state_offset + 2)]

			# Unused section
			if state == b"\x07\x00":
				break

			section_states[section] = state

		return section_states

	@staticmethod
	def _parse_device_number_from_state_packet(packet: bytes) -> int:
		return int(Jablotron._bytes_to_int(packet[4:6]) / 64)

	@staticmethod
	def _convert_jablotron_device_state_to_state(packet: bytes, device_number: int) -> Optional[str]:
		state = Jablotron._bytes_to_int(packet[3:4])

		if device_number <= 32:
			high_device_number_offset = 0
		elif device_number <= 96:
			high_device_number_offset = -64
		else:
			high_device_number_offset = -128

		device_states_offset = ((device_number + high_device_number_offset) * 4) + 104

		on_state = device_states_offset
		on_state_2 = device_states_offset + 1
		off_state = device_states_offset + 2

		if state == off_state:
			return STATE_OFF

		if (state == on_state or state == on_state_2):
			return STATE_ON

		return None

	@staticmethod
	def _int_to_bytes(number: int) -> bytes:
		return int.to_bytes(number, 1, byteorder=sys.byteorder)

	@staticmethod
	def _bytes_to_int(packet: bytes) -> int:
		return int.from_bytes(packet, byteorder=sys.byteorder)

	@staticmethod
	def _hex_to_bin(hex):
		dec = Jablotron._bytes_to_int(hex)
		bin_dec = bin(dec)
		bin_string = bin_dec[2:]
		bin_string = bin_string.zfill(len(hex) * 8)
		return bin_string[::-1]

	@staticmethod
	def _create_section_name(section: int) -> str:
		return "Section {}".format(section)

	@staticmethod
	def _create_section_alarm_id(section: int) -> str:
		return "section_{}".format(section)

	@staticmethod
	def _create_section_problem_sensor_id(section: int) -> str:
		return "section_problem_sensor_{}".format(section)

	@staticmethod
	def _create_section_problem_sensor_name(section: int) -> str:
		return "Problem of section {}".format(section)

	@staticmethod
	def _create_device_sensor_name(type: str, number: int) -> str:
		return "{} (device {})".format(DEVICES[type], number)

	@staticmethod
	def _create_device_problem_sensor_name(type: str, number: int) -> str:
		return "Problem of {} (device {})".format(DEVICES[type].lower(), number)

	@staticmethod
	def _create_device_sensor_id(number: int) -> str:
		return "device_sensor_{}".format(number)

	@staticmethod
	def _create_device_problem_sensor_id(number: int) -> str:
		return "device_problem_sensor_{}".format(number)

	@staticmethod
	def _convert_jablotron_alarm_state_to_alarm_state(packet: bytes) -> str:
		state = Jablotron._parse_jablotron_alarm_state(packet)

		if state["secondary"] == JABLOTRON_SECONDARY_STATE_ARMING:
			return STATE_ALARM_ARMING

		if state["secondary"] == JABLOTRON_SECONDARY_STATE_PENDING:
			return STATE_ALARM_PENDING

		if state["secondary"] == JABLOTRON_SECONDARY_STATE_TRIGGERED:
			return STATE_ALARM_TRIGGERED

		if state["primary"] == JABLOTRON_PRIMARY_STATE_ARMED_FULL:
			if state["tertiary"] == JABLOTRON_TERTIARY_STATE_ON:
				return STATE_ALARM_TRIGGERED
			else:
				return STATE_ALARM_ARMED_AWAY

		if state["primary"] == JABLOTRON_PRIMARY_STATE_ARMED_PARTIALLY:
			return STATE_ALARM_ARMED_NIGHT

		return STATE_ALARM_DISARMED

	@staticmethod
	def _convert_jablotron_alarm_state_to_problem_sensor_state(packet: bytes) -> str:
		state = Jablotron._parse_jablotron_alarm_state(packet)

		return STATE_ON if state["secondary"] == JABLOTRON_SECONDARY_STATE_PROBLEM else STATE_OFF

	@staticmethod
	def _convert_jablotron_device_state_to_problem_sensor_state(packet: bytes) -> str:
		# I did not find better detection
		return STATE_ON if packet[2:3] in [b"\x05", b"\x06", b"\x86", b"\xa8"] else STATE_OFF

	@staticmethod
	def _parse_jablotron_alarm_state(packet: bytes) -> Dict[str, int]:
		first_packet = packet[0:1]

		# Strange packet - converted to something that makes more sense
		if first_packet == "\x1b":
			first_packet = "\x13"

		number = Jablotron._bytes_to_int(first_packet)

		primary_state = number % 16
		secondary_state = int((number - primary_state) / 16)

		return {
			"primary": primary_state,
			"secondary": secondary_state,
			"tertiary": Jablotron._bytes_to_int(packet[1:2]),
		}


class JablotronEntity(Entity):
	_state: str

	def __init__(
			self,
			jablotron: Jablotron,
			control: JablotronControl,
	) -> None:
		self._jablotron: Jablotron = jablotron
		self._control: JablotronControl = control

	@property
	def should_poll(self) -> bool:
		return False

	@property
	def available(self) -> bool:
		return self._jablotron.last_update_success

	def _device_id(self) -> Optional[str]:
		return None

	@property
	def device_info(self) -> Optional[Dict[str, str]]:
		device_id = self._device_id()

		if device_id is None:
			return None

		return {
			"identifiers": {(DOMAIN, device_id)},
			"name": self._device_id(),
			"via_device": (DOMAIN, self._control.central_unit.serial_port),
		}

	@property
	def name(self) -> str:
		if self._control.friendly_name is not None:
			return self._control.friendly_name

		return self._control.name

	@property
	def unique_id(self) -> str:
		return "{}.{}.{}".format(DOMAIN, self._control.central_unit.serial_port, self._control.id)

	@property
	def state(self) -> str:
		return self._jablotron.states[self._control.id]

	async def async_added_to_hass(self) -> None:
		self._jablotron.substribe_entity_for_updates(self._control.id, self)

	def update_state(self, state: str) -> None:
		self._jablotron.states[self._control.id] = state
		self.async_write_ha_state()
