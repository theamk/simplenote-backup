#!/usr/bin/python
"""simplenote-backup --- export all simplenote contents to local directory

This script uses "nvpy" setup. See documentation for this on how to add your
username/password, it will be in file ~/.nvpy.cfg, contents:
  [nvpy]
  sn_username = user@email.com
  sn_password = passw0rd

The program reuses config file for its own options; you may add new section:
  [simplenote-backup]
  data-dir = ~/out-dir
  token-cache-file = ~/.nv-token-cache
  
data-dir specifies where the notes should be exported. Warning: all other content in
this directory wil be deleted.

token-cache-file specifies where the token will be cached. If the token is present 
and valid, then username/password are not required. The token will be updated 
automatically as needed.

The program can be set up to use 'git' to maintain the version history. Set it
up, add to your crontab, and you can have the permanent history of all your
notes. Even if the someone hacks the account and deletes everything, or if
the servers go down, it is still all in the history on your local pc!

"""

import ConfigParser
import errno
import fcntl
import hashlib
import json
import optparse
import os
import re
import subprocess
import sys
import socket
import time
import urllib2

import base64

sys.path.insert(1, os.path.join(os.path.dirname(os.path.realpath(__file__)), 'simperium-python'))

import simperium.core

# Magic values for simperium access to live database
# from: https://github.com/mrtazz/simplenote.py/blob/master/simplenote/simplenote.py
APP_ID   = 'chalk-bump-f49'
API_KEY  = base64.b64decode('YzhjMmI4NjMzNzE1NGNkYWJjOTg5YjIzZTMwYzZiZjQ=')
BUCKET   = 'note'

class OutputBusyError(Exception):
    pass

MAGIC_NAME = 'simplenote-backup'

class SimplenoteDownloader(object):
    def __init__(self, extra_config=None, verbose=0,
                 data_dir=None):
        self._api_bucket = None  # simperium "bucket" object
        self._lockfile = None
        self._lockfile_name = None
        self._config = None
        self._token_cache_file = None
        self.verbose = verbose
        self._read_config(extra_config)
        self._make_syncer()

        # on-disk/cached entries, keyed by id.
        # 'content' should be missing
        # updated by _read_existing_files, write_files
        # not modified by sync()
        self.entries = None

        # Entries which changed on server and which need saving
        # updated by sync(), cleared by write_files/_read_existing_files()
        self.updated = None

        # Set of all the files in the system
        # used to delete unused files.
        # updated by _read_existing_files()
        self.all_files = None

        # list of tuples (type, name) of all changes
        # updated by write_files()
        self.changes = list()

        # where the files are
        self.data_dir = data_dir
        if self.data_dir is None:
            if self._config.has_section('simplenote-backup'):
                self.data_dir = os.path.expanduser(
                    self._config.get(
                        'simplenote-backup', 'data-dir'))
        assert self.data_dir, 'Data directory not specified'

        # flag: ignore entries in trash?
        self.ignore_deleted = True

        # verify datadir, get lock
        self._init_datadir()

        self._read_existing_files()

    def log(self, level, msg):
        """Log levels:
         0 - errors only
         1 - output only if some records have changed
         2 - minor output even if all is the same
         3 - debug details
        """
        if level <= self.verbose:
            print >>sys.stderr, '>' + '*'*level, msg

    def _read_config(self, extra_config):
        self._config = ConfigParser.SafeConfigParser()
        home = os.path.abspath(os.path.expanduser('~'))

        # later config files overwrite earlier files
        # try a number of alternatives
        # This list is copied from nvpy: /nvpy/nvpy.py
        configs = [
                os.path.join(home, '.simplenote-backup.cfg'),
                os.path.join(home, 'nvpy.cfg'),
                os.path.join(home, '.nvpy.cfg'),
                os.path.join(home, '.nvpy'),
                os.path.join(home, '.nvpyrc') ]
        if extra_config is not None:
            configs.append(extra_config)
        names = self._config.read(configs)
        self.log(2, 'Read config files: %r' % names)

        self.sn_username = self._config.get('nvpy', 'sn_username', raw=True)
        if not self.sn_username:
            raise Exception('sn_username missing in config file, please set it.')

    def _make_syncer(self):
        try:
            self._token_cache_file = os.path.expanduser(self._config.get('simplenote-backup', 'token-cache-file'))
        except ConfigParser.Error:
            self._token_cache_file = '/var/run/user/%d/simplenote-backup-token' % os.getuid()

        # Read token
        try:
            with open(self._token_cache_file, 'r') as f:
                token = f.readline().strip()
            self.log(2, 'Read token (len %d) from %r' % (len(token), self._token_cache_file))
        except IOError as e:
            token = None
            self.log(1, 'No token cache file found: %s' % (e, ))

        # Verify token
        if token is not None:
            self._api_bucket = simperium.core.Api(APP_ID, token)[BUCKET]
            try:
                self._api_bucket.index(limit=1)
                self.log(2, 'Using stored token')
                return
            except urllib2.HTTPError as err:
                self.log(0, 'Token invalid, refreshing: %s' % (err, ))
            
        # If we are here, we need a new token                
        sn_password = self._config.get('nvpy', 'sn_password', raw=True)

        if not sn_password:
            raise Exception('Cannot sync -- no password defined, and no stored token. '
                            'Make sure "nvpy" works')
        
        auth = simperium.core.Auth(APP_ID, API_KEY)
        token = auth.authorize(self.sn_username, sn_password)
        self.log(1, 'Authorization successful, storing token')
        try:
            os.makedirs(os.dirname(self._token_cache_file))
        except:
            pass
        with open(self._token_cache_file, 'w') as f:
            print >>f, token
            print >>f, 'fetch_date=%s' % time.strftime('%FT%TZ', time.gmtime())

        self._api_bucket = simperium.core.Api(APP_ID, token)[BUCKET]

    def _init_datadir(self):
        assert os.path.isdir(self.data_dir), 'Cannot find data directory %r' % (
            self.data_dir)

        # read magic/lockfile
        self._lockfile_name = os.path.join(self.data_dir, MAGIC_NAME)
        if not os.path.exists(self._lockfile_name):
            raise Exception('Datadir not ready -- magic file missing. To fix:\n'
                            "echo '%s' > '%s'\n" % (self.sn_username,
                                                    self._lockfile_name))
        assert self._lockfile is None
        # r+ so we can lock it
        self._lockfile = open(self._lockfile_name, 'r+')
        contents = self._lockfile.read(1024).strip()
        if contents != self.sn_username:
            raise Exception(
                'Datadir invalid -- magic file %r has bad contents\n'
                'Want: %r\nHave: %r\n' % (
                    self._lockfile_name, self.sn_username, contents))

        # lock
        try:
            fcntl.lockf(self._lockfile.fileno(), fcntl.LOCK_EX|fcntl.LOCK_NB)
        except IOError as e:
            if e.errno != errno.EAGAIN:
                raise
            raise OutputBusyError(
                "Another instance is already running "
                "-- failed to get lock")


    def _read_existing_files(self):
        self.log(2, 'Reading existing data from %r' % self.data_dir)

        self.entries = dict()
        self.updated = dict()

        self.all_files = set()

        # expected size of txt file.
        # maps .txt filename to size
        txt_sizes = dict()

        for dirpath, dirnames, filenames in os.walk(self.data_dir):
            if dirpath == self.data_dir:
                try:
                    dirnames.remove('.git')
                except ValueError:
                    pass

            if len(filenames + dirnames) == 0:
                self.log(2, 'Directory is empty: %r' % dirpath)

            for abs_fn in [os.path.join(dirpath, rel_fn)
                           for rel_fn in filenames]:
                if abs_fn == self._lockfile_name:
                    continue

                assert abs_fn.startswith(self.data_dir)
                fullname = abs_fn[len(self.data_dir):].strip('/')

                self.all_files.add(fullname)

                if fullname.endswith('.json'):
                    with open(abs_fn, 'r') as f:
                        rec = json.load(f)
                    rec['filename__'] = fullname.rsplit('.', 1)[0]
                    self.entries[rec['key']] = rec
                elif fullname.endswith('.txt') and \
                        os.path.exists(abs_fn.rsplit('.', 1)[0] + '.json'):
                    # .txt with matching .json. save.
                    txt_sizes[fullname] = os.path.getsize(abs_fn)
                else:
                    self.log(1, 'Strange file found: %r' % abs_fn)
                    self.changes.append(('dejunk', fullname))

        # verify each .json has a matching .txt of proper size
        for _, val in sorted(self.entries.items()):
            expected = val['content_len__']
            found = txt_sizes.get(val['filename__'] + '.txt', -1)
            if expected != found:
                self.log(0, 'Data file damaged: %r has size %d, want %d' % (
                        val['filename__'] + '.txt', found, expected))
                # break stuff to force re-sync
                del val['content_len__']
                del val['version']

    def sync(self):
        # We could do a fancy sync here, passing in 'since' parameter, but it does not seem like 
        # it is worth it, given total download only takes a few seconds.
        all_entries = []
        mark = None
        while True:
            ret = self._api_bucket.index(data=True, mark=mark)
            mark = ret.get('mark')
            self.log(2, 'Got data, %d items, current %r, mark %r' % (len(ret['index']), ret['current'], mark))
            all_entries += ret['index']
            if mark is None:
                break

        dead = set(self.entries.keys())
        n_deleted = n_same = n_new = n_diff = 0

        for entry_raw in all_entries:
            #import pprint; pprint.pprint(entry)
            # merge envelope and contents into one dict
            entry = dict(version=entry_raw['v'], key=entry_raw['id'], **entry_raw['d'])

            self.log(4, 'procesing entry %r' % (entry['key'], ))

            if entry['deleted']:
                n_deleted += 1
                self.log(4, 'entry was deleted')
                if self.ignore_deleted:
                    continue

            dead.discard(entry['key'])

            old = self.entries.get(entry['key'], {})
            # compare with old
            same = True
            for k in set(old.keys() + entry.keys()):
                if k.endswith('__') or k == 'content':
                    # this is out special field
                    continue
                if old.get(k) != entry.get(k):
                    same = False
                    self.log(4, 'diff in field %r: old %r, new %r' %(k, old.get(k), entry.get(k)))
                    break
            # skip record if it has not changed.
            if same:
                n_same += 1
                continue

            if old:
                n_diff += 1
            else:
                n_new += 1

            self.log(3, 'Fetching content for entry %r' %
                     str(entry['key']))

            # If we wanted to save bandwidth and be more effective, we could ask for data=False above,
            # and fetch the full note here. On my account (347 notes), this reduces time from 1.8 to 1.0 sec,
            # -- so not worth it at all.

            # copy some fields from old data, use new ones for this
            for f in ['filename__', 'content_md5__', 'content_len__']:
                if f in old:
                    entry[f] = old[f]
            self.updated[entry['key']] = entry

        for key in sorted(dead):
            old = self.entries[key]
            self.log(1, 'Note %r gone (was %r)' % (
                    key, old.get('filename__')))
            old['gone__'] = True
            self.updated[key] = old

        self.log(1 if (n_diff or n_new or dead) else 2,
                 ('Sync done - %d same, %d in trash, %d changed, '
                  '%d added, %d gone') % (
                n_same, n_deleted, n_diff, n_new, len(dead)))

    def write_files(self, pretend=False):
        # generated filenames (no extensions), to prevent duplicates
        # by default, seed with all unchanged filenames.
        gen_filenames = set()
        for entry in self.entries.values():
            if entry['key'] in self.updated or entry.get('gone__'):
                continue
            gen_filenames.add(entry['filename__'])

        # write in order sorted by key.
        for key, entry in sorted(self.updated.items()):
            if entry.get('gone__'):
                # entry deleted
                del self.entries[key]
                self.changes.append(('del', entry['filename__']))
                continue

            #
            # generate path from some tags
            #
            path_tags = [sanitize_fname(t.lstrip('/'))
                         for t in entry['tags']
                         if t.startswith('/')]
            # we should normally only have one path-tag. If not,
            # we put longest first.
            path_tags.sort(key=lambda x:(len(x), x))
            # if it is pinned, we add it to the path, too
            if 'pinned' in entry['systemTags']:
                path_tags.append('pinned')
            if entry['deleted']:
                path_tags.insert(0, 'deleted')
            # get note title from the first line of text (sanitized)
            content_str = unicode(entry['content']).encode('utf-8')
            basename = sanitize_fname(
                content_str.strip().split('\n', 1)[0])
            path_tags.append(basename)

            # make the name. add characters from key if not unique
            suffix = ''
            seq = 0
            while True:
                fullname = os.path.join(*path_tags) + suffix
                if fullname not in gen_filenames:
                    break
                seq += 1
                if seq < len(key):
                    suffix = '-' + key[:seq]
                else:
                    suffix = '-%s-%d' % (key, seq-len(key))

            # write
            entry['filename__'] = fullname
            self._write_one_entry(entry, pretend=pretend)

            if key not in self.entries:
                self.changes.append(('add', entry['filename__']))
            else:
                self.changes.append(('mod', entry['filename__']))

            # update accounting
            self.entries[key] = self.updated.pop(key)

            gen_filenames.add(fullname)

        # List of 'orphaned' filenames which must be deleted
        orphaned = set(self.all_files)
        for fn in gen_filenames:
            orphaned.discard(fn + '.txt')
            orphaned.discard(fn + '.json')

        maybe_empty_dirs = set()
        for fn in sorted(orphaned):
            self.log(1, 'Deleting file: %r' % fn)
            if not pretend:
                os.remove(os.path.join(self.data_dir, fn))
            maybe_empty_dirs.add(os.path.dirname(fn))

        for fn in sorted(maybe_empty_dirs, key=lambda x: (-x.count('/'), x)):
            if fn == '':
                continue
            if pretend:
                self.log(1, 'Maybe removing dir %r' % fn)
            else:
                try:
                    os.rmdir(os.path.join(self.data_dir, fn))
                    self.log(1, 'Removed dir %r' % fn)
                except OSError as e:
                    if e.errno != errno.ENOTEMPTY:
                        raise
                    # dir not empty. Quietly ignore

        if pretend:
            self.log(1, 'Disregard all messages above, we were in pretend mode')


    def _write_one_entry(self, entry, pretend):
        content_str = unicode(entry['content']).encode('utf-8')

        content_len = len(content_str)
        content_md5 = hashlib.md5(content_str).hexdigest()

        if not pretend:
            fullname = os.path.join(self.data_dir,
                                    entry['filename__'] + '.txt')
            try:
                os.makedirs(os.path.dirname(fullname))
            except OSError as e:
                if e.errno != errno.EEXIST:
                    raise

        old_filename = self.entries.get(entry['key'], {}).get('filename__')

        ext = []
        # Write data if needed
        if (content_len != entry.get('content_len__') or
            content_md5 != entry.get('content_md5__') or
            old_filename != entry.get('filename__')):
            entry.update(content_len__=content_len,
                         content_md5__=content_md5)
            ext.append('txt')
            if not pretend:
                fullname = os.path.join(self.data_dir,
                                        entry['filename__'] + '.txt')
                with open(fullname, 'w') as f:
                    f.write(content_str)

        # create extra records for easier data viewing
        for f in ['creationDate', 'modificationDate']:
            entry[f + '_str__'] = time.strftime(
                '%F %T', time.localtime(float(entry[f])))

        # write metadata
        entry2 = dict(entry)
        entry2.pop('filename__')
        entry2.pop('content')
        msg = json.dumps(entry2, sort_keys=True, indent=1)
        ext.append('json')
        if not pretend:
            fullname = os.path.join(self.data_dir,
                                    entry['filename__'] + '.json')
            with open(fullname, 'w') as f:
                f.write(msg)    # pylint: disable=no-member

        self.log(1, 'Wrote entry %s to %r(%s)' % (
                repr(entry['key'][:8])[1:].strip("'"),
                str(entry['filename__']), ','.join(ext)))

    def verify_git(self):
        if not os.path.isdir(
            os.path.join(self.data_dir, '.git')):
            raise Exception('Data dir has no git repo. To fix:\n'
                            "git init '%s'" % self.data_dir)

    def maybe_checkin_to_git(self, pretend=False):
        if len(self.changes) == 0:
            self.log(2, 'No changes -- not doing anything with git')
            return

        # make commit name
        if len(self.changes) > 2:
            # summary: 5x add, 2x remove
            gcount = dict()
            for etype, _ in self.changes:
                gcount[etype] = gcount.get(etype, 0) + 1
            message = ', '.join('%dx %s' % (count, etype)
                                for (etype, count) in sorted(gcount.items()))
        else:
            # Individual files.
            message = ', '.join('%s: %s' % (etype, repr(ename)[1:].strip("'\""))
                                for (etype, ename) in self.changes)

        self.log(1, 'commiting with message: %s' % message)

        devnull = open('/dev/null', 'r+')
        # commit
        cmd = ['git', 'add', '--all']
        if pretend: cmd.append('--dry-run')

        cmd += ['--', '.']
        self.log(2, 'Running %r' % (cmd, ))
        subprocess.check_call(cmd, stdin=devnull, stdout=devnull,
                              cwd=self.data_dir)

        cmd = ['git', 'commit', '-m', message, '--quiet']
        if pretend:
            self.log(2, 'Would run: %r' % (cmd, ))
        else:
            self.log(2, 'Running %r' % (cmd, ))
            subprocess.check_call(cmd, stdin=devnull, cwd=self.data_dir)



MAX_NAME_LEN = 48
def sanitize_fname(p):
    """Make string filename-safe"""
    p = re.sub("[\x00-\x1F/\\\\:\"]+", "_", p).strip()
    p = re.sub(r"[_]{2,}", "_", p).strip('. _')
    if len(p) > MAX_NAME_LEN:
        p = p[:MAX_NAME_LEN] + "..."
    elif p == '':
        p = 'untitled'
    return p



def main():
    parser = optparse.OptionParser(usage='%prog [opts]',
                                   description=__doc__)
    parser.format_description = lambda _: parser.description.lstrip()

    parser.add_option('-c', '--extra-config', metavar='NAME',
                      help='Extra config file to read')
    parser.add_option('-o', '--output', metavar='DIR',
                      help='Output dir (may be specified in config). '
                      'WARNING: all contents will be replaced by syncer')
    parser.add_option('-n', '--pretend', action='store_true',
                      help='Do not actually write the files')
    parser.add_option('-v', '--verbose', action='count', default=0,
                      help='Increase output (more than once for more output)')
    parser.add_option('-g', '--git', action='store_true',
                      help='Commit results to git (assumes output dir has'
                      ' git repo)')
    parser.add_option('--print-changes', action='store_true',
                      help='Print changes overview to stdout')

    opts, args = parser.parse_args()
    if len(args):
        parser.error('No args accepted')

    try:
        sn = SimplenoteDownloader(extra_config=opts.extra_config,
                                  verbose=opts.verbose,
                                  data_dir=opts.output)
    except OutputBusyError as e:
        print >>sys.stderr, 'FATAL: %s' % e
        return 2

    # Set longish timeout for all HTTP requiests
    socket.setdefaulttimeout(60)

    if opts.git:
        sn.verify_git()
    sn.sync()
    sn.write_files(pretend=opts.pretend)

    if opts.git:
        sn.maybe_checkin_to_git(pretend=opts.pretend)

    if opts.print_changes:
        import pprint
        pprint.pprint(sn.changes)

if __name__ == '__main__':
    sys.exit(main())
