import os
import requests

WING_API_URL = "https://api-developer.wing.eu/v3"

def get_wing_token():
    email = os.getenv('WING_EMAIL')
    password = os.getenv('WING_PASSWORD')
    query = """
    mutation {
        createAccessToken(email: "%s", password: "%s") {
            accessToken
        }
    }
    """ % (email, password)
    response = requests.post(WING_API_URL, json={'query': query})
    data = response.json()
    return data['data']['createAccessToken']['accessToken']

def create_shipment(customer_name, customer_email, customer_phone,
                    address, city, postal_code, country="FR"):
    token = get_wing_token()
    headers = {'Authorization': f'Bearer {token}', 'Content-Type': 'application/json'}
    query = """
    mutation CreateShipment($input: CreateShipmentInput!) {
        createShipment(input: $input) {
            id
            trackingNumber
            trackingUrl
        }
    }
    """
    variables = {
        "input": {
            "recipient": {
                "name": customer_name,
                "email": customer_email,
                "phone": customer_phone,
                "address": {
                    "street": address,
                    "city": city,
                    "postalCode": postal_code,
                    "countryCode": country
                }
            }
        }
    }
    response = requests.post(
        WING_API_URL,
        json={'query': query, 'variables': variables},
        headers=headers
    )
    return response.json()
