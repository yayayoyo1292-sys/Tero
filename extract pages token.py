import requests

USER_TOKEN = "EAATEGfMOkHEBRuAJghanKE0HKu2HZBnPNbtdTe5Vu88gdSitM4soCsSpWs6zSGUvyMGhCHRituzZC82Tw8ewQeno40EXnkTHHqA2vnMLnAZAXEJkyLmwEOBkGBLODEtzHtpRnvDJmwyP1tyGKRixDdXqlCzyVWOTBRDOfWpBh4p7ByVXlnUeZCbCy57d5wZDZD"

response = requests.get(
    "https://graph.facebook.com/v19.0/me/accounts",
    params={"access_token": USER_TOKEN}
).json()

if "error" in response:
    print(f"❌ Error: {response['error']['message']}")
else:
    pages = response.get("data", [])
    print(f"✅ عدد الصفحات: {len(pages)}\n")
    for page in pages:
        print(f"📄 الاسم:  {page['name']}")
        print(f"🆔 ID:     {page['id']}")
        print(f"🔑 Token:  {page['access_token']}")
        print("-" * 50)