# LuxmedSniper
LUX MED appointments sniper
=======================================
Simple tool to notify about available slot in LUX MED medical care service using pushover notifications.

How to use LuxmedSniper?
--------------------
First of all create virtualenv and install Python requirements from requirements.txt

1) For each specialist create configuration file (yaml format) and save it for example as my_favourite_surgeon.yml:
```
luxmed:
  email: EMAIL
  password: PASSWORD
luxmedsniper: #                     mandatory  mandatory
  doctor_locator_id: 5*4430*-1*-1 # (cityId, serviceVariantId, facilitiesIds, doctorsIds) -1 means any.
                                  # You can get those ids by reading form data sent to https://portalpacjenta.luxmed.pl/PatientPortal/Reservations/Reservation/PartialSearch
                                  # on https://portalpacjenta.luxmed.pl/PatientPortal/Reservations/Reservation/Search by chrome dev tools
  lookup_time_days: 14 # How many days from now should script look at.
pushover:
  user_key: # Your pushover.net user key
  api_token:  # pushover.net App API Token
  message_template: "New visit! {AppointmentDate} at {ClinicPublicName} - {DoctorName}"
  title: "New Lux Med visit available!" # Pushover message topic
misc:
  notifydb: ./surgeon_data # State file used to remember which notifications has been sent already
```

2) Run it
```
nohup python3 luxmedSnip.py -c /path/to/my_favourite_surgeon.yml &
```
3) Wait for new appointment notifications in your pushover app on mobile :)!

# Warning

Please be advised that running too many queries against LuxMed API may result in locking your LuxMed account.
Breaching the 'fair use policy' for the first time locks the account temporarily for 1 day.
Breaching it again locks it indefinitelly and manual intervention with "Patient Portal Support" is required to unlock it.
