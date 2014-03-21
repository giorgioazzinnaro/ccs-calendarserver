##
# Copyright (c) 2006-2014 Apple Inc. All rights reserved.
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


import os
from twext.python.log import Logger
from twisted.cred.credentials import UsernamePassword
from twext.who.aggregate import DirectoryService as AggregateDirectoryService
from txdav.who.augment import AugmentedDirectoryService

from calendarserver.tap.util import getDBPool, storeFromConfig
from twext.who.idirectory import (
    RecordType, DirectoryConfigurationError, FieldName
)
from twext.who.ldap import DirectoryService as LDAPDirectoryService
from twext.who.util import ConstantsContainer
from twisted.python.filepath import FilePath
from twisted.python.reflect import namedClass
from twistedcaldav.config import fullServerPath
from txdav.who.delegates import DirectoryService as DelegateDirectoryService
from txdav.who.idirectory import (
    RecordType as CalRecordType,
    FieldName as CalFieldName
)
from txdav.who.xml import DirectoryService as XMLDirectoryService


log = Logger()


def directoryFromConfig(config, store=None):
    """
    Return a directory service based on the config.  If you want to go through
    AMP to talk to one of these as a client, instantiate
    txdav.dps.client.DirectoryService
    """

    # MOVE2WHO FIXME: this needs to talk to its own separate database.  In
    # fact, don't pass store=None if you already have called storeFromConfig()
    # within this process.  Pass the existing store in here.
    if store is None:
        pool, txnFactory = getDBPool(config)
        store = storeFromConfig(config, txnFactory, None)

    aggregatedServices = []


    for serviceKey in ("DirectoryService", "ResourceService"):
        serviceValue = config.get(serviceKey, None)

        if not serviceValue.Enabled:
            continue

        directoryType = serviceValue.type.lower()
        params = serviceValue.params

        # TODO: add a "test" directory service that produces test records
        # from code -- no files needed.

        if "xml" in directoryType:
            xmlFile = params.xmlFile
            xmlFile = fullServerPath(config.DataRoot, xmlFile)
            if not xmlFile or not os.path.exists(xmlFile):
                log.error("Path not found for XML directory: {p}", p=xmlFile)
            fp = FilePath(xmlFile)
            directory = XMLDirectoryService(fp)

        elif "opendirectory" in directoryType:
            from twext.who.opendirectory import (
                DirectoryService as ODDirectoryService
            )
            directory = ODDirectoryService()

        elif "ldap" in directoryType:
            if params.credentials.dn and params.credentials.password:
                creds = UsernamePassword(
                    params.credentials.dn,
                    params.credentials.password
                )
            else:
                creds = None
            directory = LDAPDirectoryService(
                params.uri,
                params.rdnSchema.base,
                creds=creds
            )

        else:
            log.error("Invalid DirectoryType: {dt}", dt=directoryType)
            raise DirectoryConfigurationError

        # Set the appropriate record types on each service
        types = []
        for recordTypeName in params.recordTypes:
            recordType = {
                "users": RecordType.user,
                "groups": RecordType.group,
                "locations": CalRecordType.location,
                "resources": CalRecordType.resource,
                "addresses": CalRecordType.address,
            }.get(recordTypeName, None)

            if recordType is None:
                log.error("Invalid Record Type: {rt}", rt=recordTypeName)
                raise DirectoryConfigurationError

            if recordType in types:
                log.error("Duplicate Record Type: {rt}", rt=recordTypeName)
                raise DirectoryConfigurationError

            types.append(recordType)

        directory.recordType = ConstantsContainer(types)
        directory.fieldName = ConstantsContainer((FieldName, CalFieldName))
        aggregatedServices.append(directory)

    #
    # Setup the Augment Service
    #
    if config.AugmentService.type:
        augmentClass = namedClass(config.AugmentService.type)
        log.info(
            "Configuring augment service of type: {augmentClass}",
            augmentClass=augmentClass
        )
        try:
            augmentService = augmentClass(**config.AugmentService.params)
        except IOError:
            log.error("Could not start augment service")
            raise
    else:
        augmentService = None

    userDirectory = None
    for directory in aggregatedServices:
        if RecordType.user in directory.recordTypes():
            userDirectory = directory
            break
    else:
        log.error("No directory service set up for users")
        raise DirectoryConfigurationError

    delegateDirectory = DelegateDirectoryService(
        userDirectory.realmName,
        store
    )
    aggregatedServices.append(delegateDirectory)

    aggregateDirectory = AggregateDirectoryService(
        userDirectory.realmName, aggregatedServices
    )
    try:
        augmented = AugmentedDirectoryService(
            aggregateDirectory, store, augmentService
        )

        # The delegate directory needs a way to look up user/group records
        # so hand it a reference to the augmented directory.
        # FIXME: is there a better pattern to use here?
        delegateDirectory.setMasterDirectory(augmented)

    except Exception as e:
        log.error("Could not create directory service", error=e)
        raise

    return augmented
