import requests
import json

CLIENT_ID = "189296"
CLIENT_SECRET = "2199beb6aee968ec9c2799c7bdc71c1fe0936ed6"
CODE = "28a8429ed352b072fa706d5cbdb635ad565bd22e"

res = requests.post(
    "https://www.strava.com/oauth/token",
    data={
        "client_id": CLIENT_ID,
        "client_secret": CLIENT_SECRET,
        "code": CODE,
        "grant_type": "authorization_code"
    }
)

print(res.json())
