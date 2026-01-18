import time
import datetime

import asyncio
import json
import requests

def time_in_range(start, end, current):
    """Returns whether current is in the range [start, end]"""
    return start <= current <= end

def set_solax_mode (mode, time, ev_charging):
	if ev_charging == True:
		print ("EV charging: Solax mode is", mode, "at time", time)
	else:
		print ("EV not charging: Solax mode is", mode, "at time", time)

def main():
	starttime = time.monotonic()
	while True:
		mode = "self_use"
		# Replace with your Home Assistant URL and Long-Lived Access Token

		ev_charging = True
		get_ev_charge_status()
		time_now = datetime.datetime.now().time()
		if ev_charging == True:
			mode = "charge"
		else:
			if time_in_range (datetime.time(0,0,0), datetime.time(5,30,0), time_now):
				mode = "charge"
			if time_in_range (datetime.time(5,30,0), datetime.time(11,30,0), time_now):
				mode = "self_use"
			if time_in_range (datetime.time(23,30,0), datetime.time(0,0,0), time_now):
				mode = "charge"
		set_solax_mode (mode, time_now, ev_charging)
		time.sleep(5 - ((time.monotonic() - starttime) % 5))

# Replace with your Ohme account credentials
if __name__ == "__main__":
    OHME_EMAIL = "iain.bate@gmail.com"
    OHME_PASSWORD = "LXYhtEur0wiZ6inV28EP"
    main()

