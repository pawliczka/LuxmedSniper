import argparse
import asyncio
import copy
import datetime
import inspect
import json
import logging
import pathlib
import shelve
import sys
import time
from fnmatch import fnmatch
from typing import Any, Callable, Coroutine

import requests
import schedule
import yaml
from loguru import logger


class LuxMedSniper:
    LUXMED_LOGIN_URL = 'https://portalpacjenta.luxmed.pl/PatientPortal/Account/LogIn'
    NEW_PORTAL_RESERVATION_URL = 'https://portalpacjenta.luxmed.pl/PatientPortal/NewPortal/terms/index'

    DICTIONARY_URL = 'https://portalpacjenta.luxmed.pl/PatientPortal/NewPortal/Dictionary'
    DICTIONARY_CITIES_URL = f'{DICTIONARY_URL}/cities'
    DICTIONARY_SERVICES_URL = f'{DICTIONARY_URL}/serviceVariantsGroups'
    DICTIONARY_FACILITIES_AND_DOCTORS = f'{DICTIONARY_URL}/facilitiesAndDoctors'

    def __init__(self, configuration_files: list[str]) -> None:
        logger.info("LuxMedSniper logger initialized")
        self.config: dict[str, Any]
        self.session: requests.Session
        self.notification_providers: list[Callable[[dict, dict], None] | Coroutine]

        self._load_configuration(configuration_files)
        self._setup_providers()
        self._create_session()
        self._log_in()

    def _load_configuration(self, configuration_files: list[str]) -> None:
        def merge(a: dict[str, Any], b: dict[str, Any], error_path: str = "") -> dict[str, Any]:
            for key in b:
                if key in a:
                    if isinstance(a[key], dict) and isinstance(b[key], dict):
                        merge(a[key], b[key], f"{error_path}.{key}")
                    elif a[key] == b[key]:
                        pass
                    else:
                        raise LuxmedSniperError(f"Conflict at {error_path}.{key}")
                else:
                    a[key] = b[key]
            return a

        self.config: dict[str, Any] = {}
        for configuration_file in configuration_files:
            configuration_path = pathlib.Path(configuration_file).expanduser()
            with configuration_path.open(encoding="utf-8") as stream:
                cf = yaml.load(stream, Loader=yaml.FullLoader)
                self.config = merge(self.config, cf)

    @staticmethod
    def _format_message(message_template: str, doctor_locator: dict[str, Any], appointment_data: dict[str, Any]) -> str:
        message_data = copy.deepcopy(appointment_data)
        message_data.update(doctor_locator)
        return message_template.format(**message_data)

    def _setup_providers(self) -> None:
        self.notification_providers = []
        providers = self.config['luxmedsniper']['notification_provider']
        if "pushover" in providers:
            pushover_client = PushoverClient(self.config['pushover']['user_key'], self.config['pushover']['api_token'])
            # pushover_client.send_message("Luxmed Sniper is running!")
            self.notification_providers.append(
                lambda doctor_locator, appointment: pushover_client.send_message(
                    self._format_message(self.config['pushover']['message_template'], doctor_locator, appointment))
            )
        if "slack" in providers:
            from slack_sdk import WebClient
            client = WebClient(token=self.config['slack']['api_token'])
            channel = self.config['slack']['channel']
            self.notification_providers.append(
                lambda doctor_locator, appointment: client.chat_postMessage(
                    channel=channel,
                    text=self._format_message(self.config['slack']['message_template'], doctor_locator, appointment)
                )
            )
        if "pushbullet" in providers:
            from pushbullet import Pushbullet
            pb = Pushbullet(self.config['pushbullet']['access_token'])
            self.notification_providers.append(
                lambda doctor_locator, appointment: pb.push_note(
                    title=self.config['pushbullet']['title'],
                    body=self._format_message(self.config['pushbullet']['message_template'], doctor_locator, appointment)
                )
            )
        if "ntfy" in providers:
            def ntfy_callback(doctor_locator, appointment):
                requests.post(
                    f"https://ntfy.sh/{self.config['ntfy']['topic']}",
                    data=self._format_message(self.config['ntfy']['message_template'], doctor_locator, appointment),
                    headers={"Tags": "hospital,pill,syringe", "Title": "New appointments"},
                    timeout=10,
                )

            self.notification_providers.append(ntfy_callback)
        if "gi" in providers:
            # noinspection PyUnresolvedReferences
            import gi
            gi.require_version('Notify', '0.7')
            # noinspection PyUnresolvedReferences
            from gi.repository import Notify
            # One time initialization of libnotify
            Notify.init("Luxmed Sniper")
            self.notification_providers.append(
                lambda doctor_locator, appointment: Notify.Notification.new(
                    self._format_message(self.config['gi']['message_template'], doctor_locator, appointment), None).show()
            )
        if "telegram" in providers:
            from telegram_send import send as t_send
            self.notification_providers.append(
                lambda doctor_locator, appointment: t_send(
                    messages=[self._format_message(self.config['telegram']['message_template'], doctor_locator, appointment)],
                    conf=self.config['telegram']['tele_conf_path'])
                )
        if "console" in providers:
            self.notification_providers.append(
                lambda doctor_locator, appointment: print(self._format_message(self.config['console']['message_template'], doctor_locator, appointment))
            )
        if "console_async" in providers:
            async def async_console_notification(doctor_locator, appointment):
                print(self._format_message(self.config['console_async']['message_template'], doctor_locator, appointment))

            self.notification_providers.append(async_console_notification)

    def _create_session(self) -> None:
        self.session = requests.Session()

    def _log_in(self) -> None:
        json_data = {
            "login": self.config["luxmed"]["email"],
            "password": self.config["luxmed"]["password"],
        }
        response = self.session.post(
            url=LuxMedSniper.LUXMED_LOGIN_URL,
            json=json_data,
            headers={"Content-Type": "application/json"},
        )
        logger.debug("Login response: {}.\nLogin cookies: {}", response.text, response.cookies)
        if response.status_code != requests.codes["ok"]:
            raise LuxmedSniperError(f"Unexpected response {response.status_code}, cannot log in")

        logger.info("Successfully logged in!")
        self.session.cookies = response.cookies
        for k, v in self.session.cookies.items():
            self.session.headers.update({k: v})

        token = json.loads(response.text)["token"]
        self.session.headers["authorization-token"] = f"Bearer {token}"

    @staticmethod
    def _parse_visits_new_portal(data, clinic_ids: list[int], doctor_ids: list[int]) -> list[dict]:
        appointments = []
        content = data.json()
        for termForDay in content["termsForService"]["termsForDays"]:
            for term in termForDay["terms"]:
                doctor = term['doctor']
                clinic_id = int(term["clinicGroupId"])
                doctor_id = int(doctor["id"])

                if doctor_ids and doctor_id not in doctor_ids:
                    continue
                if clinic_ids and clinic_id not in clinic_ids:
                    continue

                appointments.append(
                    {
                        'AppointmentDate': datetime.datetime.fromisoformat(term['dateTimeFrom']),
                        'ClinicId': term['clinicId'],
                        'ClinicPublicName': term['clinic'],
                        'DoctorName': f'{doctor["academicTitle"]} {doctor["firstName"]} {doctor["lastName"]}',
                        'ServiceId': term['serviceId']
                    }
                )
        return appointments

    def _get_appointments_new_portal(self, doctor_locator: dict[str, str]):
        logger.info(f'Get appointments for: {doctor_locator}')
        try:
            doctor_locator_id: str = doctor_locator["id"]
            (city_id, service_id, clinic_ids, doctor_ids) = doctor_locator_id.strip().split('*')
            clinic_ids = [*filter(lambda x: x != -1, map(int, clinic_ids.split(",")))]
            clinic_ids = clinic_ids + self.config["luxmedsniper"].get("facilities_ids", [])
            doctor_ids = [*filter(lambda x: x != -1, map(int, doctor_ids.split(",")))]
        except ValueError as err:
            raise LuxmedSniperError(f"DoctorLocatorID ({doctor_locator['name']}) seems to be in invalid format") from err

        lookup_days = self.config["luxmedsniper"]["lookup_time_days"]
        date_to = datetime.date.today() + datetime.timedelta(days=lookup_days)

        params = {
            "searchPlace.id": city_id,
            "searchPlace.type": 0,
            "serviceVariantId": service_id,
            "languageId": 10,
            "searchDateFrom": datetime.date.today().strftime("%Y-%m-%d"),
            "searchDateTo": date_to.strftime("%Y-%m-%d"),
            "searchDatePreset": lookup_days,
            "delocalized": "false",
        }
        if clinic_ids:
            params["facilitiesIds"] = clinic_ids
        if doctor_ids:
            params["doctorsIds"] = doctor_ids
        response = self.session.get(url=LuxMedSniper.NEW_PORTAL_RESERVATION_URL, params=params)
        logger.debug(response.text)
        return [
            *filter(
                lambda appointment: appointment["AppointmentDate"].date() <= date_to,
                self._parse_visits_new_portal(response, clinic_ids, doctor_ids),
            )
        ]

    def _get_notifydb_path(self) -> str:
        return self.config['misc']['notifydb_template'].format(email=self.config['luxmed']['email'])

    def _add_to_database(self, appointment: dict[str, Any]) -> None:
        with shelve.open(self._get_notifydb_path()) as db:
            notifications = db.get(appointment['DoctorName'], [])
            notifications.append(appointment['AppointmentDate'])
            db[appointment['DoctorName']] = notifications

    def _is_already_known(self, appointment: dict[str, Any]) -> bool:
        with shelve.open(self._get_notifydb_path()) as db:
            notifications = db.get(appointment['DoctorName'], [])
        if appointment['AppointmentDate'] in notifications:
            return True
        return False

    def _send_notification(self, doctor_locator: dict[str, Any], appointment: dict[str, Any]) -> None:
        for provider in self.notification_providers:
            try:
                result = provider(doctor_locator, appointment)
                if result is not None and asyncio.iscoroutine(result) is True:
                    asyncio.run(result)
            except Exception as e:
                logger.warning(f'Sending notification failed, reason: {e}')

    def check(self) -> None:
        doctor_locator: dict[str, Any]
        for doctor_locator in self.config['luxmedsniper']['doctor_locators']:
            if doctor_locator.get('enabled', True) is False:
                logger.info(f"Appointments skipping: {doctor_locator['name']}, disabled")
                continue
            try:
                appointments = self._get_appointments_new_portal(doctor_locator)
                if not appointments:
                    logger.info(f"No appointments found for: {doctor_locator['name']}")
                    return
                for appointment in appointments:
                    logger.info(
                        "Appointment found for: {app_name}! {AppointmentDate} at {ClinicPublicName} - {DoctorName}".format(
                            **appointment, app_name=doctor_locator['name']))
                    if not self._is_already_known(appointment):
                        self._add_to_database(appointment)
                        self._send_notification(doctor_locator, appointment)
                        logger.info(
                            "Notification sent for: {app_name}! {AppointmentDate} at {ClinicPublicName} - {DoctorName}".format(
                                **appointment, app_name=doctor_locator['name']))
                    else:
                        logger.info(f'Notification was already sent for: {doctor_locator['name']}')
            except Exception as e:
                logger.error(f'Looking for appointments for {doctor_locator} failed, reason: {e}')

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


class PushoverClient:
    def __init__(self, user_key, api_token):
        self.api_token = api_token
        self.user_key = user_key

    def send_message(self, message):
        data = {
            'token': self.api_token,
            'user': self.user_key,
            'message': message
        }
        r = requests.post('https://api.pushover.net/1/messages.json', data=data)
        if r.status_code != 200:
            raise Exception('Pushover error: %s' % r.text)


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

    loguru_config = {
        "handlers": [
            {"sink": sys.stdout, "level": "INFO"},
            {
                "sink": "debug.log",
                "format": "{time} - {message}",
                "serialize": True,
                "rotation": "1 week",
            },
        ]
    }
    logger.configure(handlers=loguru_config["handlers"])


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
        logger.error(s)


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
