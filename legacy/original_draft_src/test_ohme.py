import asyncio
from ohme import OhmeApiClient

async def main():
    # Replace with your Ohme account credentials
    client = OhmeApiClient("iain.bate@gmail.com", "LXYhtEur0wiZ6inV28EP")

    # Get charge sessions and status
    status = await client.async_get_charge_sessions()
    for session in status:
        print(f"Status: {session.status}")
        print(f"Energy Added: {session.kwh_added} kWh")
        print(f"Current Power: {session.power_kw} kW")

asyncio.run(main())

