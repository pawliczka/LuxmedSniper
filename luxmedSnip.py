import argparse
import yaml
import coloredlogs
import json
import logging
import os
import datetime
from pushbullet import Pushbullet
import shelve
import schedule
import string
import random
import requests
import time

coloredlogs.install(level="INFO")
log = logging.getLogger("main")


class LuxMedSniper:
    LUXMED_LOGIN_URL = 'https://portalpacjenta.luxmed.pl/PatientPortal/Account/LogIn'
    LUXMED_LOGOUT_URL = 'https://portalpacjenta.luxmed.pl/PatientPortal/Account/LogOn'
    MAIN_PAGE_URL = 'https://portalpacjenta.luxmed.pl/PatientPortal'
    NEW_PORTAL_RESERVATION_URL = 'https://portalpacjenta.luxmed.pl/PatientPortal/NewPortal/terms/index'

    def __init__(self, configuration_file="luxmedSniper.yaml"):
        self.log = logging.getLogger("LuxMedSniper")
        self.log.info("LuxMedSniper logger initialized")
        self._loadConfiguration(configuration_file)
        self._createSession()
        self._logIn()
        self.pb = Pushbullet(self.config['pushbullet']['api_key'])

    def _createSession(self):
        self.session = requests.session()
        self.session.headers.update({
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/72.0.3626.121 Safari/537.36'})
        self.session.headers.update(
            {'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,image/apng,*/*;q=0.8'})
        self.session.headers.update({'Referer': self.LUXMED_LOGOUT_URL})
        self.session.cookies.update({'LXCookieMonit': '1'})

    def _loadConfiguration(self, configuration_file):
        try:
            config_data = open(
                os.path.expanduser(
                    configuration_file
                ),
                'r'
            ).read()
        except IOError:
            raise Exception('Cannot open configuration file ({file})!'.format(file=configuration_file))
        try:
            self.config = yaml.load(config_data, Loader=yaml.FullLoader)
        except Exception as yaml_error:
            raise Exception('Configuration problem: {error}'.format(error=yaml_error))

    def _logIn(self):
        login_data = {'LogIn': self.config['luxmed']['email'], 'Password': self.config['luxmed']['password']}
        resp = self.session.post(self.LUXMED_LOGIN_URL, login_data)
        if resp.text.find('Nieprawidłowy login lub hasło.') != -1:
            raise Exception("Login or password is incorrect")
        self.log.info('Successfully logged in!')

    def _parseVisitsNewPortal(self, data):
        appointments = []
        content = json.loads(data)
        end_time = datetime.datetime.now() + datetime.timedelta(
            days=self.config['luxmedsniper']['lookup_time_days'])
        for day in content['termsForService']['termsForDays']:
            if end_time < datetime.datetime.fromisoformat(day['day']):
                continue
            for term in day['terms']:
                appointments.append(
                    {'AppointmentDate': '%s' % term['dateTimeFrom'], 'ClinicPublicName': term['clinic'],
                     'DoctorName': '%s %s' % (term['doctor']['firstName'], term['doctor']['lastName']),
                     'SpecialtyName': content['termsForService']['serviceVariantName'], 'AdditionalInfo': " "})
        return appointments

    def _getAppointmentsNewPortal(self):
        try:
            (cityId, serviceVariantId, facilitiesIds, doctorsIds) = self.config['luxmedsniper'][
                'doctor_locator_id'].strip().split('*')
        except ValueError:
            raise Exception('DoctorLocatorID seems to be in invalid format')
        data = {
            'cityId': cityId,
            'serviceVariantId': serviceVariantId,
            'languageId': 10,
            'searchDateFrom': datetime.datetime.now().strftime("%Y-%m-%d"),
            'searchDateTo': (datetime.datetime.now() + datetime.timedelta(
                days=self.config['luxmedsniper']['lookup_time_days'])).strftime("%Y-%m-%d"),
            'searchDatePreset': self.config['luxmedsniper']['lookup_time_days'],
            'processId': '8da70300-e1b7-4016-be85-%s' % ''.join(
                random.choices(string.ascii_lowercase + string.digits, k=12)),
            'triageResult': 3,
            'nextSearch': 'false',
            'searchByMedicalSpecialist': 'false'
        }
        if facilitiesIds != -1:
            data['facilitiesIds'] = facilitiesIds
        if doctorsIds != -1:
            data['doctorsIds'] = doctorsIds

        r = self.session.get(self.NEW_PORTAL_RESERVATION_URL, params=data)
        return self._parseVisitsNewPortal(r.text)

    def check(self):
        appointments = self._getAppointmentsNewPortal()
        if not appointments:
            self.log.info("No appointments found.")
            return
        for appointment in appointments:
            self.log.info(
                "Appointment found! {AppointmentDate} at {ClinicPublicName} - {DoctorName} ({SpecialtyName}) {AdditionalInfo}".format(
                    **appointment))
            if not self._isAlreadyKnown(appointment):
                self._addToDatabase(appointment)
                self._sendNotification(appointment)
                self.log.info(
                    "Notification sent! {AppointmentDate} at {ClinicPublicName} - {DoctorName} ({SpecialtyName}) {AdditionalInfo}".format(
                        **appointment))
            else:
                self.log.info('Notification was already sent.')

    def _addToDatabase(self, appointment):
        db = shelve.open(self.config['misc']['notifydb'])
        notifications = db.get(appointment['DoctorName'], [])
        notifications.append(appointment['AppointmentDate'])
        db[appointment['DoctorName']] = notifications
        db.close()

    def _sendNotification(self, appointment):
        self.pb.push_note(self.config['pushbullet']['title'], self.config['pushbullet']['message_template'].format(**appointment))

    def _isAlreadyKnown(self, appointment):
        db = shelve.open(self.config['misc']['notifydb'])
        notifications = db.get(appointment['DoctorName'], [])
        db.close()
        if appointment['AppointmentDate'] in notifications:
            return True
        return False


def work(config):
    try:
        luxmedSniper = LuxMedSniper(configuration_file=config)
        luxmedSniper.check()
    except Exception as s:
        log.error(s)


if __name__ == "__main__":
    log.info("LuxMedSniper - Lux Med Appointment Sniper")
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "-c", "--config",
        help="Configuration file path", default="luxmedSniper.yaml"
    )
    args = parser.parse_args()
    work(args.config)
    schedule.every(30).seconds.do(work, args.config)
    while True:
        schedule.run_pending()
        time.sleep(1)
