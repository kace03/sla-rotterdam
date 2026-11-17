"""Read-only hosted compatibility test. No credentials, state writes or emails."""
import json
import re
from curl_cffi import requests

for url in ('https://www.pararius.com/apartments/rotterdam/0-1800',
            'https://www.pararius.nl/huurwoningen/rotterdam/0-1800'):
    try:
        response = requests.get(url, impersonate='chrome', timeout=20, allow_redirects=False)
        print(json.dumps({'url':url, 'status':response.status_code,
            'bytes':len(response.content),
            'title':(re.search(r'<title[^>]*>([^<]*)',response.text,re.I).group(1) if re.search(r'<title[^>]*>([^<]*)',response.text,re.I) else ''),
            'listingCards':response.text.count('listing-search-item--for-rent') }),flush=True)
    except Exception as error:
        print(json.dumps({'url':url,'error':type(error).__name__}),flush=True)
