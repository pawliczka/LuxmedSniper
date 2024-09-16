import argparse
import datetime
import inspect
import json
import logging
import pathlib
import shelve
import sys
import time
import yaml

from typing import Any
from loguru import logger

import jsonschema
import requests
import schedule


class LuxMedSniper:
    LUXMED_LOGIN_URL = 'https://portalpacjenta.luxmed.pl/PatientPortal/Account/LogIn'
    NEW_PORTAL_RESERVATION_URL = 'https://portalpacjenta.luxmed.pl/PatientPortal/NewPortal/terms/index'

    def __init__(self, configuration_files):
        logger.info("LuxMedSniper logger initialized")
        self._loadConfiguration(configuration_files)
        self._setup_providers()
        self._createSession()
        self._logIn()

    def _createSession(self):
        self.session = requests.Session()

    def validate(self) -> None:
        schema_file = pathlib.Path("schema.json")
        with schema_file.open(encoding="utf-8") as f:
            schema = json.load(f)
        jsonschema.validate(instance=self.config, schema=schema)

    def _loadConfiguration(self, configuration_files):
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

        self.validate()

    def _logIn(self):
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

    def _parseVisitsNewPortal(self, data, clinic_ids: list[int], doctor_ids: list[int]) -> list[dict]:
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

    def _getAppointmentsNewPortal(self):
        try:
            (cityId, serviceId, clinicIds, doctorIds) = self.config['luxmedsniper'][
                'doctor_locator_id'].strip().split('*')

            clinicIds = [*filter(lambda x: x != -1, map(int, clinicIds.split(",")))]
            clinic_ids = clinicIds + self.config["luxmedsniper"].get("facilities_ids", [])

            doctor_ids = [*filter(lambda x: x != -1, map(int, doctorIds.split(",")))]
        except ValueError as err:
            raise LuxmedSniperError("DoctorLocatorID seems to be in invalid format") from err

        lookup_days = self.config["luxmedsniper"]["lookup_time_days"]
        date_to = datetime.date.today() + datetime.timedelta(days=lookup_days)

        params = {
            "searchPlace.id": cityId,
            "searchPlace.type": 0,
            "serviceVariantId": serviceId,
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
                self._parseVisitsNewPortal(response, clinic_ids, doctor_ids),
            )
        ]

    def check(self):
        appointments = self._getAppointmentsNewPortal()
        if not appointments:
            logger.info("No appointments found.")
            return
        for appointment in appointments:
            logger.info(
                "Appointment found! {AppointmentDate} at {ClinicPublicName} - {DoctorName}".format(
                    **appointment))
            if not self._isAlreadyKnown(appointment):
                self._addToDatabase(appointment)
                self._send_notification(appointment)
                logger.info(
                    "Notification sent! {AppointmentDate} at {ClinicPublicName} - {DoctorName}".format(**appointment))
            else:
                logger.info('Notification was already sent.')

    def _addToDatabase(self, appointment):
        db = shelve.open(self.config['misc']['notifydb'])
        notifications = db.get(appointment['DoctorName'], [])
        notifications.append(appointment['AppointmentDate'])
        db[appointment['DoctorName']] = notifications
        db.close()

    def _send_notification(self, appointment):
        for provider in self.notification_providers:
            provider(appointment)

    def _isAlreadyKnown(self, appointment):
        db = shelve.open(self.config['misc']['notifydb'])
        notifications = db.get(appointment['DoctorName'], [])
        db.close()
        if appointment['AppointmentDate'] in notifications:
            return True
        return False

    def _setup_providers(self) -> None:
        self.notification_providers = []

        providers = self.config['luxmedsniper']['notification_provider']

        if "pushover" in providers:
            pushover_client = PushoverClient(self.config['pushover']['user_key'], self.config['pushover']['api_token'])
            # pushover_client.send_message("Luxmed Sniper is running!")
            self.notification_providers.append(
                lambda appointment: pushover_client.send_message(
                    self.config['pushover']['message_template'].format(
                        **appointment, title=self.config['pushover']['title'])))
        if "slack" in providers:
            from slack_sdk import WebClient
            client = WebClient(token=self.config['slack']['api_token'])
            channel = self.config['slack']['channel']
            self.notification_providers.append(
                lambda appointment: client.chat_postMessage(channel=channel,
                                                            text=self.config['slack'][
                                                                'message_template'].format(
                                                                **appointment))
            )
        if "pushbullet" in providers:
            from pushbullet import Pushbullet
            pb = Pushbullet(self.config['pushbullet']['access_token'])
            self.notification_providers.append(
                lambda appointment: pb.push_note(title=self.config['pushbullet']['title'],
                                                 body=self.config['pushbullet'][
                                                     'message_template'].format(**appointment))
            )
        if "ntfy" in providers:

            def ntfy_callback(appointment):
                requests.post(
                    f"https://ntfy.sh/{self.config['ntfy']['topic']}",
                    data=self.config["ntfy"]["message_template"].format(**appointment),
                    headers={"Tags": "hospital,pill,syringe", "Title": "Nowa wizyta"},
                    timeout=10,
                )

            self.notification_providers.append(ntfy_callback)
        if "gi" in providers:
            import gi
            gi.require_version('Notify', '0.7')
            from gi.repository import Notify
            # One time initialization of libnotify
            Notify.init("Luxmed Sniper")
            self.notification_providers.append(
                lambda appointment: Notify.Notification.new(
                    self.config['gi']['message_template'].format(**appointment), None).show()
            )
        if "telegram" in providers:
            from telegram_send import send as t_send
            self.notification_providers.append(
                lambda appointment: t_send(messages=[self.config['telegram']['message_template'].format(**appointment)], conf=self.config['telegram']['tele_conf_path'])
            )

def work(config):
    try:
        luxmed_sniper = LuxMedSniper(config)
        luxmed_sniper.check()
    except LuxmedSniperError as s:
        logger.error(s)


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


def setup_logging():
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


if __name__ == "__main__":
    setup_logging()

    logger.info("LuxMedSniper - Lux Med Appointment Sniper")
    parser = argparse.ArgumentParser(formatter_class=argparse.ArgumentDefaultsHelpFormatter)
    parser.add_argument(
        "-c", "--config",
        help="Configuration file path", default=["luxmedSniper.yaml"],
        nargs="*"
    )
    parser.add_argument(
        "-d", "--delay",
        type=int, help="Delay in fetching updates [s]", default=1800
    )
    args = parser.parse_args()
    work(args.config)
    schedule.every(args.delay).seconds.do(work, args.config)
    while True:
        schedule.run_pending()
        time.sleep(1)
