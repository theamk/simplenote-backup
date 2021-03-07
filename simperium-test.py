#!/usr/bin/python -B

import sys
import os
import pprint
import argparse
import getpass
import base64
import copy

sys.path.insert(1, os.path.join(os.path.dirname(os.path.realpath(__file__)), 'simperium-python'))

import simperium.core

# from: https://github.com/mrtazz/simplenote.py/blob/master/simplenote/simplenote.py
APP_ID   = 'chalk-bump-f49'
API_KEY  = base64.b64decode('YzhjMmI4NjMzNzE1NGNkYWJjOTg5YjIzZTMwYzZiZjQ=')
BUCKET   = 'note'

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
        auth = simperium.core.Auth(APP_ID, API_KEY)
        passwd = args.password
        if passwd is None:
            passwd = getpass.getpass('Password for user %r: ' % (args.login))
        token = auth.authorize(args.login, passwd)    
        print 'got token:', token
        return
    elif args.token:
        token = args.token
    else:
        parser.error('neither login nor token given')

    api = simperium.core.Api(APP_ID, token)
    bucket = api[BUCKET]
                
    #print('buckets', bucket._request('%s/buckets' % APP_ID))

    resp = bucket.index(data=True, limit=5)
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
