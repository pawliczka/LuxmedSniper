# LuxmedSniper
LUX MED appointments sniper
=======================================
Simple tool to notify about available slot in LUX MED medical care service using pushover notifications.

How to use LuxmedSniper?
--------------------
First of all create virtualenv and install Python requirements from requirements.txt

1) For each luxmed users create configuration file (yaml format) and save it for example as luxmed_username.yml:
```
luxmed:
  email: EMAIL
  password: PASSWORD
luxmedsniper:
  doctor_locators:
    - id: 1*7409*-1*-1 # (cityId, serviceVariantId, facilitiesIds, doctorsIds) -1 means any.
      # You can get those ids by calling script with "--dump-ids" argument: python3 luxmed_sniper.py --dump-ids
      name: Your unique search name
      enabled: False  # temporary disable from searching
    - id: 1*7681*-1*-1
      name: Your unique search name 2
      enabled: True
  lookup_time_days: 14 # How many days from now should script look at.
pushover:
  user_key: # Your pushover.net user key
  api_token:  # pushover.net App API Token
  message_template: "New visit! {AppointmentDate} at {ClinicPublicName} - {DoctorName}"
  title: "New Lux Med visit available!" # Pushover message topic
misc:
  notifydb: ./notifications-{email}.db # State file used to remember which notifications has been sent already
```

2) Run it
```
nohup python3 luxmed_sniper.py -c /path/to/luxmed_john.yml &
```
or you can split the configuration into separate users/doctors/providers config files
```
nohup python3 luxmed_sniper.py -c user_config.yml luxmed_john.yml &
```
3) Wait for new appointment notifications in your pushover app on mobile :)!

# Warning

Please be advised that running too many queries against LuxMed API may result in locking your LuxMed account.
Breaching the 'fair use policy' for the first time locks the account temporarily for 1 day.
Breaching it again locks it indefinitely and manual intervention with "Patient Portal Support" is required to unlock it.
