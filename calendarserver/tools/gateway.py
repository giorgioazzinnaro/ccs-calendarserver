#!/usr/bin/env python

##
# Copyright (c) 2006-2010 Apple Inc. All rights reserved.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
# http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
##

from getopt import getopt, GetoptError
from grp import getgrnam
from pwd import getpwnam
import os
import plistlib
import sys
import xml

from twisted.internet import reactor
from twisted.internet.defer import inlineCallbacks
from twisted.python.util import switchUID
from twistedcaldav.config import config, ConfigurationError
from twistedcaldav.directory.directory import DirectoryError
from twext.web2.dav import davxml

from calendarserver.tools.util import loadConfig, getDirectory, setupMemcached, setupNotifications
from calendarserver.tools.principals import principalForPrincipalID, proxySubprincipal, addProxy, removeProxy, ProxyError, ProxyWarning

def usage(e=None):

    name = os.path.basename(sys.argv[0])
    print "usage: %s [options]" % (name,)
    print ""
    print "  TODO: describe usage"
    print ""
    print "options:"
    print "  -h --help: print this help and exit"
    print "  -f --config <path>: Specify caldavd.plist configuration path"
    print ""

    if e:
        sys.exit(64)
    else:
        sys.exit(0)


def main():

    try:
        (optargs, args) = getopt(
            sys.argv[1:], "hf:", [
                "help",
                "config=",
            ],
        )
    except GetoptError, e:
        usage(e)

    #
    # Get configuration
    #
    configFileName = None

    for opt, arg in optargs:
        if opt in ("-h", "--help"):
            usage()

        elif opt in ("-f", "--config"):
            configFileName = arg

        else:
            raise NotImplementedError(opt)

    try:
        loadConfig(configFileName)

        # Shed privileges
        if config.UserName and config.GroupName and os.getuid() == 0:
            uid = getpwnam(config.UserName).pw_uid
            gid = getgrnam(config.GroupName).gr_gid
            switchUID(uid, uid, gid)

        os.umask(config.umask)

        try:
            config.directory = getDirectory()
        except DirectoryError, e:
            respondWithError(str(e))
            return
        setupMemcached(config)
        setupNotifications(config)
    except ConfigurationError, e:
        respondWithError(e)
        return

    #
    # Read commands from stdin
    #
    rawInput = sys.stdin.read()
    try:
        plist = plistlib.readPlistFromString(rawInput)
    except xml.parsers.expat.ExpatError, e:
        respondWithError(str(e))
        return

    # If the plist is an array, each element of the array is a separate
    # command dictionary.
    if isinstance(plist, list):
        commands = plist
    else:
        commands = [plist]

    runner = Runner(config.directory, commands)
    if not runner.validate():
        return

    #
    # Start the reactor
    #
    reactor.callLater(0, runner.run)
    reactor.run()


attrMap = {
    'GeneratedUID' : { 'attr' : 'guid', },
    'RealName' : { 'attr' : 'fullName', },
    'RecordName' : { 'attr' : 'shortNames', },
    'Comment' : { 'extras' : True, 'attr' : 'comment', },
    'Description' : { 'extras' : True, 'attr' : 'description', },
    'Type' : { 'extras' : True, 'attr' : 'type', },
    'Capacity' : { 'extras' : True, 'attr' : 'capacity', },
    'Building' : { 'extras' : True, 'attr' : 'building', },
    'Floor' : { 'extras' : True, 'attr' : 'floor', },
    'Street' : { 'extras' : True, 'attr' : 'street', },
    'City' : { 'extras' : True, 'attr' : 'city', },
    'State' : { 'extras' : True, 'attr' : 'state', },
    'ZIP' : { 'extras' : True, 'attr' : 'zip', },
    'Country' : { 'extras' : True, 'attr' : 'country', },
    'Phone' : { 'extras' : True, 'attr' : 'phone', },
}

class Runner(object):

    def __init__(self, directory, commands):
        self.dir = directory
        self.commands = commands

    def validate(self):
        # Make sure commands are valid
        for command in self.commands:
            if not command.has_key('command'):
                respondWithError("'command' missing from plist")
                return False
            commandName = command['command']
            methodName = "command_%s" % (commandName,)
            if not hasattr(self, methodName):
                respondWithError("Unknown command '%s'" % (commandName,))
                return False
        return True

    @inlineCallbacks
    def run(self):
        try:
            for command in self.commands:
                commandName = command['command']
                methodName = "command_%s" % (commandName,)
                if hasattr(self, methodName):
                    (yield getattr(self, methodName)(command))
                else:
                    respondWithError("Unknown command '%s'" % (commandName,))

        except Exception, e:
            respondWithError("Command failed: '%s'" % (str(e),))
            raise

        finally:
            reactor.stop()

    # Locations

    def command_getLocationList(self, command):
        respondWithRecordsOfType(self.dir, command, "locations")

    def command_createLocation(self, command):

        kwargs = {}
        for key, info in attrMap.iteritems():
            if command.has_key(key):
                kwargs[info['attr']] = command[key]
        try:
            self.dir.createRecord("locations", **kwargs)
        except DirectoryError, e:
            respondWithError(str(e))
            return
        respondWithRecordsOfType(self.dir, command, "locations")

    def command_getLocationAttributes(self, command):
        guid = command['GeneratedUID']
        record = self.dir.recordWithGUID(guid)
        recordDict = recordToDict(record)
        # principal = principalForPrincipalID(guid, directory=self.dir)
        # recordDict['AutoSchedule'] = principal.getAutoSchedule()
        respond(command, recordDict)

    def command_setLocationAttributes(self, command):

        kwargs = {}
        for key, info in attrMap.iteritems():
            if command.has_key(key):
                kwargs[info['attr']] = command[key]
        try:
            self.dir.updateRecord("locations", **kwargs)
        except DirectoryError, e:
            respondWithError(str(e))
            return

        # principal = principalForPrincipalID(command['GeneratedUID'],
        #     directory=self.dir)
        # principal.setAutoSchedule(command.get('AutoSchedule', False))

        self.command_getLocationAttributes(command)

    def command_deleteLocation(self, command):
        kwargs = {}
        for key, info in attrMap.iteritems():
            if command.has_key(key):
                kwargs[info['attr']] = command[key]
        try:
            self.dir.destroyRecord("locations", **kwargs)
        except DirectoryError, e:
            respondWithError(str(e))
            return
        respondWithRecordsOfType(self.dir, command, "locations")

    # Resources

    def command_getResourceList(self, command):
        respondWithRecordsOfType(self.dir, command, "resources")

    def command_createResource(self, command):
        kwargs = {}
        for key, info in attrMap.iteritems():
            if command.has_key(key):
                kwargs[info['attr']] = command[key]
        try:
            self.dir.createRecord("resources", **kwargs)
        except DirectoryError, e:
            respondWithError(str(e))
            return
        respondWithRecordsOfType(self.dir, command, "resources")

    def command_getResourceAttributes(self, command):
        guid = command['GeneratedUID']
        record = self.dir.recordWithGUID(guid)
        recordDict = recordToDict(record)
        # principal = principalForPrincipalID(guid, directory=self.dir)
        # recordDict['AutoSchedule'] = principal.getAutoSchedule()
        respond(command, recordDict)

    def command_setResourceAttributes(self, command):

        kwargs = {}
        for key, info in attrMap.iteritems():
            if command.has_key(key):
                kwargs[info['attr']] = command[key]
        try:
            self.dir.updateRecord("resources", **kwargs)
        except DirectoryError, e:
            respondWithError(str(e))
            return

        # principal = principalForPrincipalID(command['GeneratedUID'],
        #     directory=self.dir)
        # principal.setAutoSchedule(command.get('AutoSchedule', False))

        self.command_getResourceAttributes(command)

    def command_deleteResource(self, command):
        kwargs = {}
        for key, info in attrMap.iteritems():
            if command.has_key(key):
                kwargs[info['attr']] = command[key]
        try:
            self.dir.destroyRecord("resources", **kwargs)
        except DirectoryError, e:
            respondWithError(str(e))
            return
        respondWithRecordsOfType(self.dir, command, "resources")

    # Proxies

    @inlineCallbacks
    def command_listWriteProxies(self, command):
        principal = principalForPrincipalID(command['Principal'], directory=self.dir)
        if principal is None:
            respondWithError("Principal not found: %s" % (command['Principal'],))
            return
        (yield respondWithProxies(self.dir, command, principal, "write"))

    @inlineCallbacks
    def command_addWriteProxy(self, command):
        principal = principalForPrincipalID(command['Principal'],
            directory=self.dir)
        if principal is None:
            respondWithError("Principal not found: %s" % (command['Principal'],))
            return

        proxy = principalForPrincipalID(command['Proxy'], directory=self.dir)
        if proxy is None:
            respondWithError("Proxy not found: %s" % (command['Proxy'],))
            return
        try:
            (yield addProxy(principal, "write", proxy))
        except ProxyError, e:
            respondWithError(str(e))
            return
        except ProxyWarning, e:
            pass
        (yield respondWithProxies(self.dir, command, principal, "write"))

    @inlineCallbacks
    def command_removeWriteProxy(self, command):
        principal = principalForPrincipalID(command['Principal'], directory=self.dir)
        if principal is None:
            respondWithError("Principal not found: %s" % (command['Principal'],))
            return
        proxy = principalForPrincipalID(command['Proxy'], directory=self.dir)
        if proxy is None:
            respondWithError("Proxy not found: %s" % (command['Proxy'],))
            return
        try:
            (yield removeProxy(principal, proxy, proxyTypes=("write",)))
        except ProxyError, e:
            respondWithError(str(e))
            return
        except ProxyWarning, e:
            pass
        (yield respondWithProxies(self.dir, command, principal, "write"))

    @inlineCallbacks
    def command_listReadProxies(self, command):
        principal = principalForPrincipalID(command['Principal'], directory=self.dir)
        if principal is None:
            respondWithError("Principal not found: %s" % (command['Principal'],))
            return
        (yield respondWithProxies(self.dir, command, principal, "read"))

    @inlineCallbacks
    def command_addReadProxy(self, command):
        principal = principalForPrincipalID(command['Principal'], directory=self.dir)
        if principal is None:
            respondWithError("Principal not found: %s" % (command['Principal'],))
            return
        proxy = principalForPrincipalID(command['Proxy'], directory=self.dir)
        if proxy is None:
            respondWithError("Proxy not found: %s" % (command['Proxy'],))
            return
        try:
            (yield addProxy(principal, "read", proxy))
        except ProxyError, e:
            respondWithError(str(e))
            return
        except ProxyWarning, e:
            pass
        (yield respondWithProxies(self.dir, command, principal, "read"))

    @inlineCallbacks
    def command_removeReadProxy(self, command):
        principal = principalForPrincipalID(command['Principal'], directory=self.dir)
        if principal is None:
            respondWithError("Principal not found: %s" % (command['Principal'],))
            return
        proxy = principalForPrincipalID(command['Proxy'], directory=self.dir)
        if proxy is None:
            respondWithError("Proxy not found: %s" % (command['Proxy'],))
            return
        try:
            (yield removeProxy(principal, proxy, proxyTypes=("read",)))
        except ProxyError, e:
            respondWithError(str(e))
            return
        except ProxyWarning, e:
            pass
        (yield respondWithProxies(self.dir, command, principal, "read"))


@inlineCallbacks
def respondWithProxies(directory, command, principal, proxyType):
    proxies = []
    subPrincipal = proxySubprincipal(principal, proxyType)
    if subPrincipal is not None:
        membersProperty = (yield subPrincipal.readProperty(davxml.GroupMemberSet, None))
        if membersProperty.children:
            for member in membersProperty.children:
                proxyPrincipal = principalForPrincipalID(str(member))
                proxies.append(proxyPrincipal.record.guid)

    respond(command, {
        'Principal' : principal.record.guid, 'Proxies' : proxies
    })


def recordToDict(record):
    recordDict = {}
    for key, info in attrMap.iteritems():
        try:
            if info.get('extras', False):
                value = record.extras[info['attr']]
            else:
                value = getattr(record, info['attr'])
            recordDict[key] = value
        except KeyError:
            pass
    return recordDict

def respondWithRecordsOfType(directory, command, recordType):
    result = []
    for record in directory.listRecords(recordType):
        recordDict = recordToDict(record)
        result.append(recordDict)
    respond(command, result)

def respond(command, result):
    sys.stdout.write(plistlib.writePlistToString( { 'command' : command['command'], 'result' : result } ) )

def respondWithError(msg, status=1):
    sys.stdout.write(plistlib.writePlistToString( { 'error' : msg, } ) )
    """
    try:
        reactor.stop()
    except RuntimeError:
        pass
    sys.exit(status)
    """

if __name__ == "__main__":
    main()
