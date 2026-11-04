#!/usr/bin/env python3

import pprint
import argparse
import getpass
import base64
import copy

import requests

# from: https://github.com/mrtazz/simplenote.py/blob/master/simplenote/simplenote.py
APP_ID   = 'chalk-bump-f49'
API_KEY  = base64.b64decode('YzhjMmI4NjMzNzE1NGNkYWJjOTg5YjIzZTMwYzZiZjQ=').decode('ascii')
BUCKET   = 'note'
AUTH_URL = 'https://auth.simperium.com/1/%s/authorize/' % APP_ID
INDEX_URL = 'https://api.simperium.com/1/%s/%s/index' % (APP_ID, BUCKET)
REQUEST_TIMEOUT = 60

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument(
        '-l', '--login', metavar='USER',
        help='Perform a login process, print token and exit')
    parser.add_argument(
        '-p', '--password',
        help='Set a password for login (default ask)')

    parser.add_argument(
        '-t', '--token', metavar='TOKEN',
        help='Use existing token')

    args = parser.parse_args()

    if args.login:
        assert not args.token
        passwd = args.password
        if passwd is None:
            passwd = getpass.getpass('Password for user %r: ' % (args.login))
        response = requests.post(AUTH_URL, data={
            'client_id': API_KEY,
            'username': args.login,
            'password': passwd,
        }, timeout=REQUEST_TIMEOUT)
        response.raise_for_status()
        token = response.json()['access_token']
        print('got token:', token)
        return
    elif args.token:
        token = args.token
    else:
        parser.error('neither login nor token given')

    response = requests.get(INDEX_URL, params={
        'data': '1',
        'limit': 5,
    }, headers={'X-Simperium-Token': token}, timeout=REQUEST_TIMEOUT)
    response.raise_for_status()
    resp = response.json()
    index = resp.pop('index')
    print('Result meta: %r' % (resp, ))
    for item in index:
        assert set(item.keys()) == set(('id', 'v', 'd')), item.keys()
        d1 = copy.copy(item['d'])
        d1['__meta__'] = (item['v'], item['id'])
        d1['content'] = '[%d] %s...' % (len(d1['content']), d1['content'][:100])
        for k, v in list(d1.items()):
            if not v: del d1[k]
        pprint.pprint(d1)


if __name__ == '__main__':
    main()
