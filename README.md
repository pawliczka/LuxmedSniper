# LuxmedSniper
LUX MED appointments sniper
=======================================
Simple tool to notify about available slot in LUX MED medical care service using pushover notifications.

How to use LuxmedSniper?
--------------------
First of all create virtualenv and install python requirements from requirements.txt

1) For each specialist create configuration file (yaml format) and save it for example as my_favourite_surgeon.yml:
```
luxmed:
  email: EMAIL
  password: PASSWORD
luxmedsniper: #                     mandatory  mandatory
  doctor_locator_id: 5*4430*-1*-1 # (city_id, service_id, clinic_id, doctor_multi_identyfier) -1 means any.
                                  # You can get those ids by reading form data sent to https://portalpacjenta.luxmed.pl/PatientPortal/Reservations/Reservation/PartialSearch
                                  # on https://portalpacjenta.luxmed.pl/PatientPortal/Reservations/Reservation/Search by chrome dev tools
  lookup_time_days: 60 # How many days from now should script look at.
pushbullet:
  api_key: "o.SETmU9muaH4Mi1QUiqIkCLcTVcycJo3N" # Your Pushbullet api key
  message_template: "New visit! {AppointmentDate} at {ClinicPublicName} - {DoctorName} ({SpecialtyName}) {AdditionalInfo}"
  title: "New Lux Med visit available!" # Pushbullet message topic
misc:
  notifydb: ./surgeon_data # State file used to remember which notifications has been sent already
  max_number_of_visits: 3
```

2) Run it
```
nohup python3 luxmedSnip.py -c /path/to/my_favourite_surgeon.yml &
```
3) Wait for new appointment notifications in your pushbullet app on mobile :)!
