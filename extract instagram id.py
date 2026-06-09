import requests

PAGE_TOKEN = "EAATEGfMOkHEBRk5Bt1G5UNiOZArwvOXwW0J7I4XWt1OpG6DHmqCcebsZCBgO9MjNU9bZAdHgqHnnZCZALCKnETIGe8QMzwb8KXF3cap7dqifWXz9yJU0eatflaN9x40gvXbUcaije8NYRESIEI6s5IDNak5ceC2v9V8ElZAPFJRbfnrqS9hd2C1TSXZBoFmk9kEnCwe"
PAGE_ID = "1124851357382211"

r = requests.get(
    f"https://graph.facebook.com/v19.0/{PAGE_ID}",
    params={
        "fields": "instagram_business_account",
        "access_token": PAGE_TOKEN
    }
).json()

if "error" in r:
    print(f"❌ Error: {r['error']['message']}")
elif "instagram_business_account" in r:
    ig_id = r["instagram_business_account"]["id"]
    print(f"✅ Instagram Business Account ID: {ig_id}")
else:
    print("⚠️ مفيش حساب انستا مربوط بالصفحة دي")