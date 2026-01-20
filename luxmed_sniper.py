import argparse
import asyncio
import copy
import datetime
import inspect
import json
import logging
import os
import subprocess
import sys
import time
import typing
import uuid
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from fnmatch import fnmatch
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import requests
import schedule
import yaml
from loguru import logger, BasicHandlerConfig, FileHandlerConfig
from pydantic import BaseModel, Field, model_validator


class DoctorLocator(BaseModel):
    id: str
    name: str | None = None
    enabled: bool = True

    city_id: str
    service_id: str
    clinic_ids: list[int] = Field(default_factory=list)
    doctor_ids: list[int] = Field(default_factory=list)

    @model_validator(mode="before")
    @classmethod
    def _parse_from_id_and_config(cls, data: Any, info):
        if not isinstance(data, dict):
            return data

        raw_id = str(data.get("id", "")).strip()
        try:
            city_id, service_id, clinic_ids_str, doctor_ids_str = raw_id.split("*")
        except ValueError as e:
            raise ValueError(
                "doctor_locator.id must have format: cityId*serviceId*facilitiesIds*doctorsIds (example: 1*7409*-1*-1)",
            ) from e

        def _parse_ids(s: str) -> list[int]:
            return [i for i in map(int, s.split(",")) if i != -1]

        clinic_ids = _parse_ids(clinic_ids_str)
        doctor_ids = _parse_ids(doctor_ids_str)

        luxmedsniper_config = (info.context or {}).get("luxmedsniper_config", {}) if info else {}
        clinic_ids += list(luxmedsniper_config.get("facilities_ids", []) or [])

        return {
            **data,
            "city_id": city_id,
            "service_id": service_id,
            "clinic_ids": clinic_ids,
            "doctor_ids": doctor_ids,
            "name": data.get("name") or raw_id,
            "enabled": data.get("enabled", True),
        }


@dataclass
class Appointment:
    AppointmentDate: datetime.datetime
    clinic_id: int
    ClinicPublicName: str
    DoctorName: str
    service_id: int


class Luxmed(BaseModel):
    password: str
    login: str | None = None
    email: str | None = None

    model_config = {"validate_assignment": True}

    @model_validator(mode="before")
    @classmethod
    def _validate_login_or_email(cls, data: Any) -> Any:
        if not isinstance(data, dict):
            return data

        login = data.get("login")
        email = data.get("email")

        # exactly one of login/email must be provided
        if bool(login) == bool(email):
            raise ValueError("Provide exactly one of: luxmed.login or luxmed.email")

        # email is deprecated but still supported
        if email and not login:
            logger.warning("Config field luxmed.email is deprecated; use luxmed.login instead.")
            data["login"] = email

        return data


class LuxmedSniper(BaseModel):
    doctor_locators: list[DoctorLocator]
    lookup_time_days: int
    facilities_ids: list[int]
    notification_provider: list[str]


class LuxMedSniper:
    LUXMED_LOGIN_URL = 'https://portalpacjenta.luxmed.pl/PatientPortal/Account/LogIn'
    NEW_PORTAL_RESERVATION_URL = 'https://portalpacjenta.luxmed.pl/PatientPortal/NewPortal/terms/index'

    DICTIONARY_URL = 'https://portalpacjenta.luxmed.pl/PatientPortal/NewPortal/Dictionary'
    DICTIONARY_CITIES_URL = f'{DICTIONARY_URL}/cities'
    DICTIONARY_SERVICES_URL = f'{DICTIONARY_URL}/serviceVariantsGroups'
    DICTIONARY_FACILITIES_AND_DOCTORS = f'{DICTIONARY_URL}/facilitiesAndDoctors'

    def __init__(self, configuration_files: typing.Iterable[str] = tuple("luxmedSniper.yaml")):
        logger.info("LuxMedSniper logger initialized")

        self.config = LuxMedSniper._load_configuration(configuration_files)
        self.notification_providers = self._setup_providers()
        self.session = requests.Session()

        self._log_in()

    @staticmethod
    def _load_configuration(configuration_files: Iterable[str]) -> dict[str, Any]:
        def merge(a: dict[str, Any], b: dict[str, Any], error_path: str = "") -> dict[str, Any]:
            for kb, vb in b.items():
                va = a.get(kb, vb)
                if va == vb:
                    a[kb] = va
                elif isinstance(a[kb], dict) and isinstance(vb, dict):
                    merge(a[kb], vb, f"{error_path}.{kb}")
                else:
                    err_msg = f"Conflict at {error_path}.{kb}"
                    raise LuxmedSniperError(err_msg)
            return a

        config: dict[str, Any] = {}
        for configuration_file in configuration_files:
            configuration_path = Path(configuration_file).expanduser()
            with configuration_path.open(encoding="utf-8") as stream:
                cf = yaml.safe_load(stream)
                config = merge(config, cf)

        logger.debug("Configuration: {}", config)

        return config

    @staticmethod
    def _format_message(message_template: str, doctor_locator: DoctorLocator, appointment_data: Appointment) -> str:
        message_data = copy.deepcopy(appointment_data.__dict__)
        message_data.update(doctor_locator.__dict__)
        return message_template.format(**message_data)

    def _setup_providers(self) -> list[Callable[[DoctorLocator, Appointment], Any]]:  # noqa: C901
        notification_providers: list[Callable[[DoctorLocator, Appointment], Any]] = []

        providers = self.config["luxmedsniper"]["notification_provider"]

        if "pushover" in providers:
            pushover_user_key = self.config["pushover"]["user_key"]
            pushover_api_token = self.config["pushover"]["api_token"]

            def pushover_callback(doctor_locator: DoctorLocator, appointment: Appointment) -> None:
                message = self.config["pushover"]["message_template"].format(**appointment.__dict__)
                data = {
                    "token": pushover_api_token,
                    "user": pushover_user_key,
                    "message": message,
                    "title": self.config["pushover"]["title"],
                }
                response = requests.post("https://api.pushover.net/1/messages.json", data=data, timeout=10)
                if response.status_code != requests.codes["ok"]:
                    err_msg = f"Pushover error: {response.text}"
                    raise LuxmedSniperError(err_msg)

            notification_providers.append(pushover_callback)
        if "slack" in providers:
            from slack_sdk import WebClient  # noqa: PLC0415

            client = WebClient(token=self.config["slack"]["api_token"])
            channel = self.config["slack"]["channel"]
            notification_providers.append(
                lambda doctor_locator, appointment: client.chat_postMessage(
                    channel=channel,
                    text=self.config["slack"]["message_template"].format(vars(appointment)),
                ),
            )
        if "pushbullet" in providers:
            from pushbullet import Pushbullet  # noqa: PLC0415

            pb = Pushbullet(self.config["pushbullet"]["access_token"])

            notification_providers.append(
                lambda doctor_locator, appointment: pb.push_note(
                    title=self.config["pushbullet"]["title"],
                    body=self.config["pushbullet"]["message_template"].format_map(vars(appointment)),
                ),
            )
        if "ntfy" in providers:
            def ntfy_callback(doctor_locator, appointment):
                requests.post(
                    f"https://ntfy.sh/{self.config['ntfy']['topic']}",
                    data=self.config["ntfy"]["message_template"].format_map(vars(appointment)),
                    headers={"Tags": "hospital,pill,syringe", "Title": "Nowa wizyta"},
                    timeout=10,
                )

            notification_providers.append(ntfy_callback)
        if "gi" in providers:
            import gi  # noqa: PLC0415

            gi.require_version("Notify", "0.7")
            from gi.repository import Notify  # noqa: PLC0415

            # One time initialization of libnotify
            Notify.init("Luxmed Sniper")
            notification_providers.append(
                lambda doctor_locator, appointment: Notify.Notification.new(
                    self.config["gi"]["message_template"].format_map(vars(appointment)),
                    None,
                ).show(),
            )
        if "telegram" in providers:
            from telegram_send import send as t_send  # noqa: PLC0415

            notification_providers.append(
                lambda doctor_locator, appointment: t_send(
                    messages=[self.config["telegram"]["message_template"].format_map(vars(appointment))],
                    conf=self.config["telegram"]["tele_conf_path"],
                ),
            )
        if "sound" in providers:
            audio_file = Path(self.config["sound"]["audio"])

            def cb(_, __):
                subprocess.check_output(["/usr/bin/aplay", audio_file], shell=False, stderr=subprocess.STDOUT)  # noqa: S603

            notification_providers.append(cb)
        if "console" in providers:
            notification_providers.append(
                lambda doctor_locator, appointment: print(
                    self._format_message(self.config["console"]["message_template"], doctor_locator, appointment),
                ),
            )
        if "console_async" in providers:
            async def async_console_notification(doctor_locator, appointment):
                print(
                    self._format_message(
                        self.config["console_async"]["message_template"], doctor_locator, appointment
                    ),
                )

            notification_providers.append(async_console_notification)

        return notification_providers

    def _log_in(self) -> None:
        luxmed = Luxmed(**self.config["luxmed"])
        json_data = {
            "login": luxmed.login,
            "password": luxmed.password,
        }
        response = self.session.post(
            url=LuxMedSniper.LUXMED_LOGIN_URL,
            json=json_data,
            headers={"Content-Type": "application/json"},
        )
        logger.debug("Login response: {}.\nLogin cookies: {}", response.text, response.cookies)
        if response.status_code != requests.codes["ok"]:
            err_msg = f"Unexpected response {response.status_code}, cannot log in"
            raise LuxmedSniperError(err_msg)

        logger.info("Successfully logged in!")
        self.session.cookies = response.cookies
        for k, v in self.session.cookies.items():
            self.session.headers.update({k: v})

        token = json.loads(response.text)["token"]
        self.session.headers["authorization-token"] = f"Bearer {token}"

    @staticmethod
    def _parse_visits_new_portal(data: requests.Response, doctor_locator_id: DoctorLocator) -> list[Appointment]:
        appointments = []
        content = data.json()
        for term_for_day in content["termsForService"]["termsForDays"]:
            for term in term_for_day["terms"]:
                doctor = term["doctor"]
                clinic_id = int(term["clinicGroupId"])
                doctor_id = int(doctor["id"])

                if doctor_locator_id.doctor_ids and doctor_id not in doctor_locator_id.doctor_ids:
                    continue
                if doctor_locator_id.clinic_ids and clinic_id not in doctor_locator_id.clinic_ids:
                    continue

                appointments.append(
                    Appointment(
                        datetime.datetime.fromisoformat(term["dateTimeFrom"]).replace(
                            tzinfo=ZoneInfo("Europe/Warsaw"),
                        ),
                        term["clinicId"],
                        term["clinic"],
                        f"{doctor['academicTitle']} {doctor['firstName']} {doctor['lastName']}",
                        term["serviceId"],
                    ),
                )
        return appointments

    def _get_appointments_new_portal(self, doctor_locator: DoctorLocator) -> list[Appointment]:
        logger.info(f"Get appointments for: {doctor_locator}")

        lookup_days = self.config["luxmedsniper"]["lookup_time_days"]
        date_to = datetime.datetime.now(tz=datetime.UTC) + datetime.timedelta(days=lookup_days)

        params = {
            "searchPlace.id": doctor_locator.city_id,
            "searchPlace.type": 0,
            "serviceVariantId": doctor_locator.service_id.split(",")[0],
            "languageId": 10,
            "searchDateFrom": datetime.datetime.now(tz=datetime.UTC).date().strftime("%Y-%m-%d"),
            "searchDateTo": date_to.strftime("%Y-%m-%d"),
            "searchDatePreset": lookup_days,
            "delocalized": "false",
            "processId": str(uuid.uuid4()),
        }

        if doctor_locator.clinic_ids:
            params["facilitiesIds"] = doctor_locator.clinic_ids
        if doctor_locator.doctor_ids:
            params["doctorsIds"] = doctor_locator.doctor_ids

        logger.debug("Parameters: {}", params)

        response = self.session.get(url=LuxMedSniper.NEW_PORTAL_RESERVATION_URL, params=params)
        logger.debug(response.text)
        return [
            *filter(
                lambda appointment: appointment.AppointmentDate <= date_to,
                LuxMedSniper._parse_visits_new_portal(response, doctor_locator),
            ),
        ]

    def _add_to_database(self, appointment: Appointment) -> None:
        path = Path(self.config["misc"]["notifydb"])
        if path.exists():
            try:
                with path.open(encoding="utf-8") as f:
                    db: dict[str, list[str]] = json.load(f)
            except json.JSONDecodeError:
                db = {}  # corrupted file → reset
        else:
            db = {}

        # 2. Update DB
        notifications = db.get(appointment.DoctorName, [])
        notifications.append(appointment.AppointmentDate.isoformat())
        db[appointment.DoctorName] = notifications

        # class DateTimeEncoder(json.JSONEncoder):
        #     def default(self, obj):
        #         if isinstance(obj, datetime.datetime):
        #             return obj.isoformat()
        #         return super().default(obj)

        # 3. Save back to JSON (atomic write)
        with path.open("w", encoding="utf-8") as f:
            # json.dump(db, f, cls=DateTimeEncoder, indent=2, ensure_ascii=False)
            json.dump(db, f, indent=2, ensure_ascii=False)

    def _is_already_known(self, appointment: Appointment) -> bool:
        path = Path(self.config["misc"]["notifydb"])

        # If file doesn't exist → nothing known yet
        if not path.exists():
            return False

        try:
            with path.open(encoding="utf-8") as f:
                db: dict[str, list[str]] = json.load(f)
        except json.JSONDecodeError:
            logger.exception("Database file corrupted, treating as empty")
            return False

        notifications = map(datetime.datetime.fromisoformat, db.get(appointment.DoctorName, []))
        return appointment.AppointmentDate in notifications

    def _send_notification(self, doctor_locator: DoctorLocator, appointment: Appointment) -> None:
        for provider in self.notification_providers:
            try:
                result = provider(doctor_locator, appointment)
                if result is not None and asyncio.iscoroutine(result) is True:
                    asyncio.run(result)
            except Exception as e:
                logger.exception(f"Sending notification failed, reason: {e}")

    def check(self) -> None:
        doctor_locator_dict: dict[str, Any]
        for doctor_locator_dict in self.config["luxmedsniper"]["doctor_locators"]:
            doctor_locator = DoctorLocator.model_validate(
                doctor_locator_dict,
                context={"luxmedsniper_config": self.config["luxmedsniper"]},
            )

            if not doctor_locator.enabled:
                logger.info(f"Appointments skipping: {doctor_locator.name}, disabled")
                continue
            try:
                appointments = self._get_appointments_new_portal(doctor_locator)
                if not appointments:
                    logger.info(f"No appointments found for: {doctor_locator.name}")
                    continue
                for appointment in appointments:
                    logger.info(
                        "Appointment found for: {app_name}! {AppointmentDate} at {ClinicPublicName} - {DoctorName}".format(
                            **appointment.__dict__,
                            app_name=doctor_locator.name,
                        ),
                    )
                    if not self._is_already_known(appointment):
                        self._add_to_database(appointment)
                        self._send_notification(doctor_locator, appointment)
                        logger.info(
                            "Notification sent for: {app_name}! {AppointmentDate} at {ClinicPublicName} - {DoctorName}".format(
                                **appointment.__dict__,
                                app_name=doctor_locator.name,
                            ),
                        )
                    else:
                        logger.info(f"Notification was already sent for: {doctor_locator.name}")
            except Exception as e:
                logger.exception(f"Looking for appointments for {doctor_locator} failed, reason: {e}")

    def get_cities(self) -> list[dict]:
        response = self.session.get(
            url=LuxMedSniper.DICTIONARY_CITIES_URL,
            headers={"Content-Type": "application/json"},
        )
        return response.json()

    def get_services(self) -> list[dict]:
        response = self.session.get(
            url=LuxMedSniper.DICTIONARY_SERVICES_URL,
            headers={"Content-Type": "application/json"},
        )
        return response.json()

    def get_facilities_and_doctors(self, city_id: int, service_variant_id: int) -> dict[str, Any]:
        params = {
            'cityId': city_id,
            'serviceVariantId': service_variant_id
        }
        response = self.session.get(
            url=LuxMedSniper.DICTIONARY_FACILITIES_AND_DOCTORS,
            params = params,
            headers={"Content-Type": "application/json"}
        )
        return response.json()


class LuxmedSniperError(Exception):
    pass


def setup_logging() -> None:
    class InterceptHandler(logging.Handler):
        def emit(self, record: logging.LogRecord) -> None:
            # Get corresponding Loguru level if it exists.
            level: str | int
            try:
                level = logger.level(record.levelname).name
            except ValueError:
                level = record.levelno

            # Find caller from where originated the logged message.
            frame, depth = inspect.currentframe(), 0
            while frame and (depth == 0 or frame.f_code.co_filename == logging.__file__):
                frame = frame.f_back
                depth += 1

            logger.opt(depth=depth, exception=record.exc_info).log(level, record.getMessage())

    logging.basicConfig(handlers=[InterceptHandler()], level=0, force=True)
    requests_log = logging.getLogger("urllib3")
    requests_log.setLevel(logging.DEBUG)
    requests_log.propagate = True

    handlers = [
        BasicHandlerConfig(sink=sys.stdout, level=os.environ.get("LOGURU_LEVEL", "INFO")),
        FileHandlerConfig(sink="debug.log", format="{time} - {message}", serialize=True,rotation="1 week"),
    ]
    logger.configure(handlers=handlers)


# noinspection PyTypeChecker
def dump_current_ids(config, city_wildcard: str | None, dump_ids_doctors: bool) -> None:
    luxmed_sniper = LuxMedSniper(config)
    cities: list[dict[str, Any]] = luxmed_sniper.get_cities()
    logger.info(f'Found: {len(cities)} cities')
    services: list[dict[str, Any]] = []
    for s in luxmed_sniper.get_services():
        services.append(
            dict(id=s['id'],name=s['name'], telemedicine=s['isTelemedicine'])
        )
        for c in s['children']:
            services.append(
                dict(id=c['id'], name=c['name'], telemedicine=c['isTelemedicine'])
            )
            for c2 in c['children']:
                services.append(
                    dict(id=c2['id'], name=c2['name'], telemedicine=c2['isTelemedicine'])
                )
    logger.info(f'Found: {len(services)} services')
    facilities_and_doctors = {}  # per city, per service
    for city in cities:
        if city_wildcard is not None:
            if not fnmatch(city['name'], city_wildcard):
                logger.info(f'{city["name"]} - skipping facilities and doctors')
                continue
        if dump_ids_doctors is False:
            continue
        logger.info(f'{city["name"]} - looking for facilities and doctors')
        facilities_and_doctors[city['id']] = {}
        for service in services:
            facilities_and_doctors[city['id']][service['id']] = {}
            fac_and_doc: dict[str, Any] = luxmed_sniper.get_facilities_and_doctors(city['id'], service['id'])
            facilities_and_doctors[city['id']][service['id']]['facilities'] = copy.deepcopy(fac_and_doc['facilities'])
            facilities_and_doctors[city['id']][service['id']]['doctors'] = [
                dict(id=d['id'], name=f'{d["academicTitle"]} {d["lastName"]} {d["firstName"]}')
                for d in fac_and_doc['doctors']
            ]

    json.dump(cities, open('luxmed-ids/ids-cities.json', 'w', encoding='utf-8'), indent=4, ensure_ascii=False)
    json.dump(services, open('luxmed-ids/ids-services.json', 'w', encoding='utf-8'), indent=4, ensure_ascii=False)
    json.dump(facilities_and_doctors, open('luxmed-ids/ids-facilities-doctors.json', 'w', encoding='utf-8'), indent=4, ensure_ascii=False)


def work(config: list[str]) -> None:
    try:
        luxmed_sniper = LuxMedSniper(config)
        luxmed_sniper.check()
    except LuxmedSniperError as s:
        logger.exception(s)


if __name__ == "__main__":
    setup_logging()
    logger.info("Lux Med Appointment Sniper")
    parser = argparse.ArgumentParser(formatter_class=argparse.ArgumentDefaultsHelpFormatter)
    parser.add_argument(
        "-c", "--config",
        help="Configuration file path", default=["luxmed_sniper.yaml"],
        nargs="*"
    )
    parser.add_argument(
        "-d", "--delay",
        type=int, help="Delay in fetching updates [s]", default=1800
    )
    group = parser.add_argument_group('dump-ids')
    group.add_argument(
        "--dump-ids",
        action='store_true', dest='dump_ids', help="Dump current ids", default=False
    )
    group.add_argument(
        "--dump-ids-city",
        type=str, dest='dump_ids_city', help="Dump facilities and doctors only from this city (wildcard)", default=None
    )
    group.add_argument(
        "--dump-ids-doctors",
        action='store_true', dest='dump_ids_doctors', help="Dump facilities and doctors also (many requests)", default=False
    )
    args = parser.parse_args()

    if args.dump_ids is True:
        logger.info(f'Dumping IDs')
        dump_current_ids(args.config, args.dump_ids_city, args.dump_ids_doctors)
    else:
        logger.info(f'Start working every: {args.delay} s')
        work(args.config)
        schedule.every(args.delay).seconds.do(work, args.config)
        try:
            while True:
                schedule.run_pending()
                time.sleep(1)
        except KeyboardInterrupt:
            ...
    logger.info('Exiting')
