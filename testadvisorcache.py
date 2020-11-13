# -*- coding: utf-8 -*-
import json
import logging
import os
from datetime import datetime
from uuid import uuid4

import redis
from redis.exceptions import RedisError


from jsondiff import diff

log = logging.getLogger(__name__)

"""
Environment

The values in the following dictionary are the names of enviroment variables.
Each entry is as per the following format:
    <module variable>: <environment variable>
"""
env_keys = {
    "redis_host": "INFRA_ADVICE_CACHE_HOST",
    "redis_port": "INFRA_ADVICE_CACHE_PORT",
    "redis_db": "INFRA_ADVICE_CACHE_DATABASE",
}

"""This module provides a Python interface to the Test Advisor cache.

Data used to track the progress of Test Advisor recommendations is cached
in a Redis cluster. Methods on TestAdvisorCache and CommitEvent instances
provide read and write functionality for the cache.

Below is the schema used to cache data in the cache. Python classes in this
module are used to represent the cache data in Python scripts and applications.

# JSON Entry                                # Python Object
------------------------------------------  ----------------
{                                           # CommitEvent
    "uid": <str>,
    "event_time: <timestamp>,
    "recommendations": [
        {           # Recommendation
            "time" : {                      # Time
                "timestamp" : <datetime>
            },
            "comment" : {                   # Comment
                "task" : {                  # Task
                    "task_id" : <int>,
                    "task_status" : <str>,
                    "task_data" : <str>
                },
                "comment_id" : <int>,
                "advice" : <str>
            },
            "auto_test_run" : <bool>,
            "test" : {                      # Test
                "test_job_name" : <str>,
                "test_job_number" : <int>,
                "test_job_url" : <str>,
                "start" : <datetime>,
                "finish" : <datetime>,
                "test_result" : <str>
            },
            "build": {                      # Build
                "link" : <str>,
                "result: <str>
            }
        ]
   }
}

Example:
    # Create an entry

    cache = TestAdvisorCache(redis_host='16.100.209.56',
                             redis_port=7990,
                             redis_db=2)

    comment = Comment(commit_id=672929, auto_test_run=False)
    recommendations = [Recommendation(comment=comment, time=datetime.utcnow())]

    task = Task(task_id=42215,
                task_data=mystr,
                task_status='OPEN'
               )
    comment = Comment(commit_id=672932, auto_test_run=True, task=task)
    test = Test(test_job_name=job_name,
                job_num=jenkins_job_num,
                test_result=None,
                start=test_start,
                finish=None)
    recommendations.append(Recommendation(comment=comment,
                                          test=test,
                                          time=datetime.utcnow()))

    entry = CommitEvent(cache, uid, recommendations)
    entry.save()

    # Update an entry with test results:
    entry = cache.get_commit_event(uid)
    for recs in entry.recommendation()
    entry.recommendations
"""
# # # Descriptor Classes


class DatetimeAttr:
    """A descriptor that provides pseudo serialization for datetime objects.

    This descriptor is designed to use with the JsonRepr class. JsonRepr
    objects implement a json() method that produces a json representation
    of the object's data.

    This descriptor provides serialization/deserialization of JsonRepr object
    attributes since the Python json module cannot serialize datetime objects.

    Setter:
        The setter accepts datetime objects and strings that can be parsed
        into datetime objects.

        The setter saves the data to the attribute in string form. The string
        form is typically used when saving json data to a Redis cache.

        A value of None can also be assigned to the attribute.

    Getter:
        The getter returns a datetime object reflecting the
        data in the attribute. Or, None if the attribute does not contain
        time data.

    Raises:
        TypeError: The data being assigned to the attribute
            is not a datetime object or a string.
        ValueError: The string being assigned to the attribute
            cannot be converted to a datetime object.
    """

    def __get__(self, instance, owner):
        value = instance.__dict__[self.name]
        if value is None:
            return None
        return parsedate(value)

    def __set__(self, instance, value):
        if value is None:
            instance.__dict__[self.name] = None
        else:
            if isinstance(value, datetime):
                # The data is stored as a string since a datetime object
                # cannot be serialized to JSON for the cache.
                instance.__dict__[self.name] = str(value)
            elif isinstance(value, str):
                # Induces a ValueError for un-parsable strings.
                # We do not want to assign bad data to the attribute.
                parsedate(value)
                instance.__dict__[self.name] = value
            else:
                raise TypeError(
                    f"Incorrect type for {self.name}. "
                    "Expected a datetime instance or "
                    "a valid string representation of a timestamp."
                )

    def __set_name__(self, owner, name):
        self.name = name


class AutoAttr:
    """A descriptor that manages data for most attributes.

    The descriptor is used to assign instances of primitive data types and
    class instances of classes in this module. The descriptor
    can be configured to type-check value assignments.

    Type checking is activated if the data type is specified when the
    descriptor is assigned to an attribute, for example:
        advice = AutoAttr(str)
        test = AutoAttr(Test)

    Setter:
        The setter assigns a value to an attribute and, optionally,
        validates the incoming data against a required data type.

        The exception is for None. This allows the attribute to be initialized
        or to be reset to None if code requires it.

    Getter:
        The getter behaves like the default Python getters. That is,
        the getter simply returns the value of an attribute.

    Raises:
        TypeError: When type-checking is activated and the data being
            assigned to an attribute is of the wrong type.
    """

    def __init__(self, type_needed=None):
        self._type_needed = type_needed

    def __get__(self, instance, owner):
        return instance.__dict__[self.name]

    def __set__(self, instance, value):
        if (
            value is not None
            and self._type_needed is not None
            and not isinstance(value, self._type_needed)
        ):
            raise TypeError(
                f"Incorrect type for {self.name}. "
                f"Expected {self._type_needed}"
            )

        instance.__dict__[self.name] = value

    def __set_name__(self, owner, name):
        self.name = name


class SetOnceAttr:
    """A descriptor for attributes that become immutable once set.

    Certain attributes indicate a specific instance of an object whose value
    came from an external source, e.g. a task_id from Bitbucket. Modifying
    an attribute can corrupt cache entries by changing the relationships
    to or between external objects.

    Type-validation is required when using this descriptor. The data type for
    the attribute is specified when assigning this descriptor
    to an attribute, for example:
            uid = SetOnceAttr(str)
            comment_id = SetOnceAttr(int)

    Setter:
        The setter validates the incoming data against the required data type.

        The attribute can be initialized to None. However, the attribute
        cannot be assigned back to None once a valid value has been set.

    Getter
        The getter behaves like the default Python getters. That is,
        the getter simply returns the value of an attribute.

    Raises:
        TypeError: When type-checking is activated and the data being
            assigned to an attribute is of the wrong type.
        ValueError: When a value cannot be assigned to an attribute that has
            already been assigned a value.
    """

    def __init__(self, type_needed):
        if type_needed is None:
            # If this error is not caught during initialization, a confusing
            # error is raised when the setter is called. That error message
            # indicates an isinstance() failure in the code below.
            raise TypeError(
                "__init__() None is not a valid value for the "
                "required positional argument. The attribute must be "
                "declard with a type, e.g. SetOnceAttr(str), SetOnceAttr(Test)"
            )
        self._type_needed = type_needed

    def __get__(self, instance, owner):
        return instance.__dict__[self.name]

    def __set__(self, instance, value):
        read_only_violation = False
        is_id_set = (
            True
            if isinstance(
                instance.__dict__.get(self.name, None), self._type_needed
            )
            else False
        )
        if value is None:
            if is_id_set:
                # Do not allow this sequence:
                #   None (init) -> a value of the correct type
                #    -> None (reset) -> a different value of the correct type
                read_only_violation = True
            else:
                instance.__dict__[self.name] = None
        else:
            if isinstance(value, self._type_needed):
                if is_id_set:
                    read_only_violation = True
                else:
                    instance.__dict__[self.name] = value
            else:
                raise TypeError(
                    f"Incorrect type for {self.name}. "
                    f"Expected {self._type_needed}"
                )
        if read_only_violation:
            raise ValueError(
                f"Cannot modify a read-only attribute: {self.name}."
            )

    def __set_name__(self, owner, name):
        self.name = name


# # # Test Advisor Classes


class TestAdvisorCache(object):
    """An interface to the Test Advisor cache.

    This class provides public methods to retrieve data from the Test Advisor
    cache.

    Each entry in the cache represents the top commit in a Git repository
    associated with a pull request (PR) in Bitbucket. The 'comment event' may
    occur when a PR is opened or updated by a push of a new commit.

    Each commit event is stored and retrieved by an key containing information
    about the pull request associated with the commit and is in the format:
        <project>:<repository>:<PR number>:<commit>

    In addition to retrieving commit events, the TestAdvisorCache is used to
    persist Test Advisor data usig CommitEvent objects.

    TestAdvisorCache objects will be bound to a cache server during
    initialization if the host, port, and db information is supplied when
    creating the instance.

    TestAdvisorCache objects can also be bound to a cache server
    after initialization using the bind() method.

    Args:
        **kwargs:
            host (str): The IP address or hostname of the server providing the
                cache service for TestAdvisor.
            port (int): The port number used by the host.
            db (int): The database within the cache.

    Environment:
        Environment variables can be used to bind to the Test Advisor cache
        during initialization. Environment variables are overriden by
        arguments supplied during object instantiation.

        Environment variables:
            env_keys['redis_host']
            env_keys['redis_port']
            env_keys['redis_db']

    Raises:
        RedisError:
            An operation failed on the cache.
        ValueError:
            A supplied value was not useable, e.g. a non-int port number.
        AttributeError:
            A required parameter is missing.
    """

    __test__ = False

    def __init__(self, **kwargs):
        # The global env_keys is used in this method
        params = {}
        for key in ["redis_host", "redis_port", "redis_db"]:
            # Method call parameters are the first choice.

            params[key] = kwargs.get(key, None)
            if params[key] is None:
                # Fall back on environment variables
                params[key] = os.getenv(env_keys[key])
                if params[key] is None:
                    raise AttributeError(
                        f"The {key} must be specified "
                        "in order to bind to the cache."
                    )
        try:
            self.bind(
                redis_host=params["redis_host"],
                redis_port=int(params["redis_port"]),
                redis_db=int(params["redis_db"]),
            )
        except ValueError:
            # Python was unable to convert the text to an integer.
            # Replace the exception message with a more meaningful message.
            raise ValueError(
                f"Integers are required for the db and port parameters."
            )

    def bind(self, redis_host, redis_port, redis_db):
        """Bind to a cache server.

        Subsequent operations with the TestAdvisorCache object are directed to
        the bound server.

        Args:
            host (str): The IP address or hostname of the server providing the
                cache service for TestAdvisor.
            port (int): The port number used by the host.
            db (int): The database within the cache.

        Returns:
            None

        Raises:
            The bind operation does not raise exceptions.
            Rather, exceptions are realized when operations are performed on
            the cache.
        """
        self._redis = redis.Redis(
            host=redis_host,
            port=redis_port,
            db=redis_db,
            decode_responses=True,
        )
        # Don't return the connection object.
        return None

    def delete_entry(self, uids):
        """Delete one or more entries from the Test Advisor cache.

        Args:
            uids (str,list): One or more entries specified by its/their uid(s).

        Returns:
            None

        Raises:
            A RedisError is raised if the operation fails.
        """
        self._redis.delete(uids)

    def get_commit_events(self, pattern="*", raw_json=False):
        """Retrieves cache entries for commits whose key matches the pattern.

        Args:
            pattern:  The pattern is applied to keys (uid's) for entries
                in the Redis cache. Characters used in searches are compliant
                with the Redis CLI and programmatic interfaces. Redis seems
                to use something akin to shell globbing. The '*' wildcard
                matches all characters and and '?' matches a single character.
                See Redis documentation about 'key' and 'scan' functionality
                for details.
            raw_json: Setting to True will result in JSON being returned
                rather than Commit Event objects.

        Returns:
            By default, a list containing one or more CommitEvent objects
            is returned. All events that match the key pattern are
            included.

            When the raw_json argument is set to True, one or more JSON
            objects are returned.

            An empty list is returned if no matches are found.

        Raises:
            A RedisError is raised if the operation fails.
        """
        entries = []
        keys = self.keys(pattern)
        for key in keys:
            entries.append(self.get_commit_event(key, raw_json))

        return entries

    def get_commit_event(self, key, raw_json=False):
        """Retrieve the commit event indicated by key (uid).

        Args:
            key (str): The key used to identify the commit event entry.

        Returns:
            By default, a CommitEvent is returned for the specified key.

            When the raw_json argument is set to True, a JSON objects
            is returned.

            None is returned if there is no entry for the provided key.

        Raises:
            A RedisError is raised if the operation fails.
        """
        entry = self._redis.get(key)
        if entry is None:
            return None
        else:
            entry = json.loads(entry)

            if raw_json:
                return entry

        ce = TACacheFactory.make("commitevent", **entry)
        ce.bind_to_cache(bind_tac=self)
        return ce

    def has_entry(self, key):
        """Verifies if an entry exists in the cache or not.

        Args:
        Args:
            key (str): The key used to identify the commit event entry.

        Returns:
            True if there is an entry for the key. False if there is no entry.

        Raises:
            A RedisError is raised if the operation fails.
        """
        return True if self.keys(key) else False

    def keys(self, pattern="*"):
        """Retrieve one or more keys from the cache.

        The pattern is appplied against the key format:
            <project>:<repository>:<PR number>:<commit>

        Args:
            pattern (str): Similar to shell globbing. The '*' wildcard
                matches all characters and and '?' matches a single character.

        Returns:
            A list of matching keys.

        Raises:
            A RedisError is raised if the operation fails.
        """
        # The scan() method is more efficient than the keys() method.
        # The scan() method returns a list containing the cursor positon and
        # a list of matching keys.
        return self._redis.scan(cursor=0, match=pattern, count=200000)[1]

    def repo_keys(self, project, repo_name):
        """Retrieve keys for repository(ies) in a Bitbucket project.

        Args:
            project (str):
                The project key for the Bitbucket project. There are no
                spaces in the project key.
            repo_name (str): The 'short name' of the Bitbucket repository.
                There are no spaces in the repo name.

        Returns:
            A list of matching keys. None, if no keys match.

        Raises:
           A RedisError is raised if the operation fails.
        """
        # Keys that match a Bitbucket repository.
        return self.keys(f"{project}:{repo_name}:*")


class JsonRepr:
    """
    Returns:
        A JSON object containing the data in the object.

        This is the form that the data is stored in the cache.
    """

    # Standard attributes for built-in Python data types.
    _attrs_std = []
    # Child instances of this class export JSON using the json() method.
    _attrs_json = []
    # Attributes that are used in the Python interface to the object
    # but not exported via json. The intended use is for attributes
    # that are not meant to be saved to the cache using the
    # JSON information for a child object.
    _attrs_json_omit = []

    def __init__(self, **kwargs):
        for attr in self._attrs_std + self._attrs_json:
            setattr(self, attr, kwargs.get(attr, None))

    def __str__(self):
        return f'{{"{self._name}": {json.dumps(self.json())}}}'

    def __eq__(self, other):
        if not diff(self.json(), other.json()):
            return True
        return False

    def _process_standard_attributes(self):
        jdata = {}
        for attr in self._attrs_std:
            try:
                value = getattr(self, attr)
            except KeyError:
                continue

            if value is not None:
                if isinstance(value, datetime):
                    # A datetime instance
                    # cannot be serialized by the json module.
                    jdata[attr] = str(value)
                else:
                    jdata[attr] = value
        return jdata

    def _process_json_attributes(self):
        jdata = {}
        for attr in self._attrs_json:
            if attr in self._attrs_json_omit:
                continue
            try:
                value = getattr(self, attr)
            except KeyError:
                continue

            if value is not None:
                if isinstance(value, list):
                    # Lists do not have a json() method.
                    jdata[attr] = []
                    for item in value:
                        jdata[attr].append(item.json())
                else:
                    jdata[attr] = value.json()
        return jdata

    def json(self):
        json_out = self._process_standard_attributes()
        json_out.update(self._process_json_attributes())
        return json_out


class Test(JsonRepr):
    """ A test executed by Test Advisor.

    Args:
        **kwargs: JSON information for a test run by Test Advisor.

    Test Attributes:
        test_job_number (int): The job number applied to the test by Jenkns.
        test_job_name (str): The job name in Jenkins.
        test_result (str): The outcome of the test.
        test_job_url (str): The location of the Jenkins job summary.
        test_labels (list): A list of labesl associated with this test job.
        start (datetime): When the test started. The datetime object is
            in UTC and does not have timezone information.
        finish (datetime):  When the test completed. The datetime object is
            in UTC and does not have timezone information.
    """

    __test__ = False

    _attrs_std = [
        "test_job_number",
        "test_job_name",
        "test_job_url",
        "test_labels",
        "test_result",
        "start",
        "finish",
    ]

    test_job_number = AutoAttr(int)
    test_job_name = AutoAttr()
    test_job_url = AutoAttr()
    test_labels = AutoAttr(list)
    test_result = AutoAttr()
    start = DatetimeAttr()
    finish = DatetimeAttr()

    def __init__(self, **kwargs):
        self._name = "test"
        super().__init__(**kwargs)


class CommitEvent(JsonRepr):
    """The information that resulted from updates to a
    pull request in Bitbucket.

    Each update to a pull request (PR) triggers the Test Advisor. The Test
    Advisor may make one or more recommendations based on the content
    of the commit.

    This object provides the only entry point to write to the
    Test Advisor cache. The cache is written to when an instance of this
    object is saved. This is true for creating new cache entries as well as
    updating existing entries.

    CommitEvent objects can be initialize with or without parameters.
    Generally, the uid, event_time, host, port, and db settings are used when
    instantiating Commitevent oobjects in scripts. The cache_json parameter
    is used by automation for uid, event_time, and recommendations data.

    To update existing cache entries, retrieve a CommitEvent from the
    TestAdvisorCache using the entry's uid. Then, update the relevant object(s)
    and save the CommitEvent instance.

    Args:
        **kwargs: JSON information for a entry in the Redis cache.
            host (str):
                The IP address or hostname of the server providing the
                cache service for TestAdvisor.
            port (int):
                The port number used by the host.
            db (int):
                The database within the cache.
            bind_tac (TestAdvisorCache):
                In lieu of supplying the host,
                port, and db information, An existing TestAdvisorCache instance
                can be bound to this object.
            bind_env (bool):
                Bind to the cache host specified in the environment.

    Attributes:
        uid:
            The unique key that identifies a commit event in the Test Advisor
            cache. The uid is in the format:
                <project>:<repository>:<PR number>:<commit>

            The uid is a critical identifier. The uid attribute becomes
            immutable once a value is assigned.
        project:
            The Bitbucket project derived from the uid. This attribute
            cannot be changed once it is set.
        repo:
            The Bitbucket repository derived from the uid.
            This attribute cannot be changed once it is set.
        pr_num:
            The integer for the Bitbucket pull request derived from the uid.
            This attribute cannot be changed once it is set.
        commit:
            The commit derived from the uid. This is the HEAD of the Git
            repository that applies to all objects and actions associated
            with this CommitEvent instance. This attribute cannot be changed
            once it is set.
        event_time (datetime):
            The time that the commit event occurred in timezone naive UTC.
        recommendations:
            A list of Recommendation objects associated with this commit event.

    Raises:
        ValueError:
            When uid is in an incorrect format is applied to an object.
        AttributeError:
            When an attempt is made to write to an immutable attribute.
            Or, when an attribute/parameter is missing or incorrect.
        RedisError:
            When there is a problem on an operation performed on the
            Redis cache back-end.
        TypeError:
            A parameter of the wrong type was passed to a CommitEvent
            method.
    """

    _attrs_std = ["event_time", "uid"]
    _attrs_json = ["recommendations"]
    # These attributes in _attrs_json_omit are available on
    # CommitEvent objects but are not exported using the json() method.
    # These attributes are not persisted to the cache.
    _attrs_json_omit = ["commit", "pr_num", "project", "repo"]

    commit = SetOnceAttr(str)
    event_time = DatetimeAttr()
    pr_num = SetOnceAttr(int)
    project = SetOnceAttr(str)
    recommendations = AutoAttr(list)
    repo = SetOnceAttr(str)
    uid = SetOnceAttr(str)

    def __init__(self, **kwargs):
        self._name = "commitevent"
        super().__init__(**kwargs)
        # It is safe to call bind_to_cache without bind-specific parameters.
        self.bind_to_cache(**kwargs)

    def __setattr__(self, name, value):
        # The call to the setter in the super
        # is a call to the setter assigned by the descriptor.
        if name == "uid":
            if value is None:
                super(CommitEvent, self).__setattr__("uid", None)
            elif isinstance(value, str):
                super(CommitEvent, self).__setattr__("uid", value)
                (project, repo, pr, commit) = value.split(":")
                super(CommitEvent, self).__setattr__("project", project)
                super(CommitEvent, self).__setattr__("repo", repo)
                super(CommitEvent, self).__setattr__("pr_num", int(pr))
                super(CommitEvent, self).__setattr__("commit", commit)
            else:
                raise TypeError(
                    "Incorrect type for uid. "
                    "Expected: None or <class 'str'>"
                )
        else:
            super(CommitEvent, self).__setattr__(name, value)

    def bind_to_cache(self, **kwargs):
        """Bind this CommitEvent object to a cache host.

        There are multiple options for binding to the Redis cache. The options
        are exercised depending on paramters used during initialization.

        The options are evaluated in the order listed below.
        When parameters satisfy an option, no more options are evaluated.

        Bind Options:
           1. Pass in the bind_tac parameter.
           2. Pass in the host, port, and db parameters for a cache host.
           3. Assign True to the bind_env parameter to use cache host
              parameters in the execution environment.

        Args:
            **kwargs: JSON information for a entry in the Redis cache.
                bind_tac (TestAdvisorCache):
                    In lieu of supplying the host,
                    port, and db information, An existing TestAdvisorCache
                    instance can be bound to this object.

                redis_host (str):
                    The IP address or hostname of the server providing the
                    cache service for TestAdvisor.
                redis_port (int):
                    The port number used by the host.
                redis_db (int):
                    The database within the cache.

                bind_env (bool):
                    Retrieve the network parameters for the cache
                    host from the environment.

        Returns:
            None

        Raises:
            AttributeError:
                An AttributeError occurs when network information is passed
                in but one of the following three parameter keys is misssing:
                host, port, db
            RedisError:
                A RedisError is raised if the bind operation fails.
            TypeError:
                A TypeError is the the bind_tac key is passed in but
                is not assigned to a TestAdvisorCache instance.
        """
        bound_cache = None
        while bound_cache is None:

            # Option 1: A direct import
            bind_tac = kwargs.get("bind_tac", None)
            if bind_tac is not None:
                if isinstance(bind_tac, TestAdvisorCache):
                    bound_cache = bind_tac
                    # Skip other bind options.
                    break
                else:
                    raise TypeError(
                        f"Invalid bind_tac parameter: {bind_tac} "
                        "is not a TestAdvisorCache instance."
                    )

            # Option 2: function call parameters
            redis_host = kwargs.get("redis_host", None)
            redis_port = kwargs.get("redis_port", None)
            redis_db = kwargs.get("redis_db", None)

            # Option 3: Fall back to using the environment if the host
            # parameters were not set and the bind_env parameter is set.
            bind_env = kwargs.get("bind_env", False)
            if not all([redis_host, redis_port, redis_db]) and bind_env:
                # User-supplied parameters have priority over environment vars.
                redis_host = os.getenv(env_keys["redis_host"])
                redis_port = os.getenv(env_keys["redis_port"])
                redis_db = os.getenv(env_keys["redis_db"])

            # Use either the parameters or environment variable values
            # to create a TestAdvisorCache instance.
            if all([redis_host, redis_port, redis_db]):
                try:
                    bound_cache = TestAdvisorCache(
                        redis_host=redis_host,
                        redis_port=int(redis_port),
                        redis_db=int(redis_db),
                    )
                    break

                except ValueError:
                    raise AttributeError(
                        f"Each of the port ({redis_port}) and db ({redis_db}) "
                        "for the database, must be an integer or a string "
                        "that can be converted to an integer."
                    )
            elif any([redis_host, redis_port, redis_db]):
                raise AttributeError(
                    f"All three parameters, host ({redis_host}), "
                    f"port({redis_port}), and db({redis_db}) "
                    "must be provided in order to bind to a cache host."
                )

            # Option 4: No information supplied for the cache host.
            # This can happen during initiation.
            bound_cache = None
            break

        if (bind_tac or bind_env) and bound_cache is None:
            raise RedisError(
                "Unknown error. Could not bind to cache. "
                "Verify that bind_tac or bind_env are prepared correctly."
            )

        self._test_advisor_cache = bound_cache
        return True

    def find_build(self, **kwargs):
        """Locate a build within this CommitEvent.

        Recommendations are searched for a build that is co-located
        with a specified comment or task.

        Both or either a comment and task may be specified.

        Args:
            **kwargs:
                comment_id (int): Locate a build in the
                    same recommendation as the specified comment.
                task_id (int): Locate a build in the
                    same recommendation as the specified task.
        Returns:
            If a match is found, the Build object is returned.
        """
        comment_id = kwargs.get("comment_id", None)
        recommendation = self._find_rec_by_comment_id(comment_id)
        if recommendation is not None:
            return recommendation.build

        task_id = kwargs.get("task_id", None)
        recommendation = self._find_rec_by_task_id(task_id)
        if recommendation is not None:
            return recommendation.build

        return None

    def find_comment(self, **kwargs):
        """Locate a comment within this CommitEvent.

        If the comment_id is specified, the comment is searched for. If a
        task_id is specified, the search locates the comment that
        is associated with the task.

        Args:
            **kwargs:
                comment_id (int): Locate the comment in this CommitEvent.
                task_id (int): Locate the comment that is associated
                    with the task.

        Returns:
            If a match is found, the Comment object is returned.
            If the comment is not found, None is returned.
        """
        comment_id = kwargs.get("comment_id", None)
        recommendation = self._find_rec_by_comment_id(comment_id)
        if recommendation is not None:
            return recommendation.comment

        task_id = kwargs.get("task_id", None)
        recommendation = self._find_rec_by_task_id(task_id)
        if recommendation is not None:
            return recommendation.comment

        return None

    def find_recommendation(self, **kwargs):
        """Locate a recommendation within this CommitEvent.
​
        The recommendation that contains the comment or task specified.
​
        Both or either a recommendation and task may be specified.
​
        Args:
            **kwargs:
                advice (str): Locate recommendations with matching advice.
                comment_id (int): Locate the recommendation in which the
                    comment is located.
                job_name (str):  Locate recommendations with
                    matching job names.
                task_id (int): Locate the recommendation in which the
                    task is located.
                index (bool): See returns.
        Returns:
            By default , if a match is found, the Recommendation object(s)
            is/are returned. In the cases of a matching comment_id or task_id,
            the single matching recommendation is returned. For matching
            advice or job_name's, a list is returned.

            If an index parameter is passed in as True, the the index or
            indices of the matching recommendation(s) in self.recommendations
            is/are returned.
        """

        index = kwargs.get("index", False)

        advice = kwargs.get("advice", None)
        if advice is not None:
            return self._find_rec_by_advice(advice, index)

        comment_id = kwargs.get("comment_id", None)
        if comment_id is not None:
            return self._find_rec_by_comment_id(comment_id, index)

        recommendation_id = kwargs.get("recommendation_id", None)
        if recommendation_id is not None:
            return self._find_rec_by_recommendation_id(
                recommendation_id, index
            )

        task_id = kwargs.get("task_id", None)
        if task_id is not None:
            return self._find_rec_by_task_id(task_id, index)

        test_job_name = kwargs.get("test_job_name", None)
        if test_job_name is not None:
            return self._find_rec_by_test_job_name(test_job_name, index)

        return None

    def _find_rec_by_advice(self, advice=None, index=False):
        if advice is None:
            return None

        # TODO: Handle multiple matches. And, update Advisor code as needed.
        # OMNI-83196
        # matches = []
        for idx in range(0, len(self.recommendations)):
            rec = self.recommendations[idx]
            try:
                if rec.comment.advice == advice:
                    if index is True:
                        return idx
                        # matches.append(idx)
                    else:
                        return rec
                        # matches.append(rec)
            except AttributeError:
                # For example, an "empty" Recommendation object.
                pass

        # return matches
        return None

    def _find_rec_by_comment_id(self, comment_id=None, index=False):
        if comment_id is None:
            return None

        for idx in range(0, len(self.recommendations)):
            rec = self.recommendations[idx]
            try:
                if rec.comment.comment_id == comment_id:
                    if index is True:
                        return idx
                    else:
                        return rec
            except AttributeError:
                # For example, an "empty" Recommendation object.
                pass

        return None

    def _find_rec_by_recommendation_id(
        self, recommendation_id=None, index=False
    ):
        if recommendation_id is None:
            return None

        for idx in range(0, len(self.recommendations)):
            rec = self.recommendations[idx]
            try:
                if recommendation_id == rec.recommendation_id:
                    if index is True:
                        return idx
                    else:
                        return rec
            except AttributeError:
                # For example, an "empty" Recommendation object.
                pass

        return None

    def _find_rec_by_task_id(self, task_id=None, index=False):
        if task_id is None:
            return None

        for idx in range(0, len(self.recommendations)):
            rec = self.recommendations[idx]
            try:
                if rec.comment.task.task_id == task_id:
                    if index is True:
                        return idx
                    else:
                        return rec
            except AttributeError:
                # Not all recommendations have tasks.
                pass

        return None

    def _find_rec_by_test_job_name(self, test_job_name=None, index=False):
        if test_job_name is None:
            return None

        # TODO: Handle multiple matches. And, update Advisor code as needed.
        # OMNI-83196
        # matches = []
        for idx in range(0, len(self.recommendations)):
            rec = self.recommendations[idx]
            try:
                if rec.test.test_job_name == test_job_name:
                    if index is True:
                        return idx
                        # matches.append(idx)
                    else:
                        return rec
                        # matches.append(rec)
            except AttributeError:
                # Not all recommendations have tests.
                pass

        # return matches
        return None

    def find_task(self, **kwargs):
        """Locate a task within this CommitEvent.

        If the task_id is specified, the task is searched for.
        If a comment_id is specified, the search locates
        the task that is associated with the comment.

        Args:
            **kwargs:
                comment_id (int): Locate the task that is associated
                    with the comment.
                task_id (int): Locate the comment in this CommitEvent.

        Returns:
            If a match is found, the task object is returned.
            If no match is found, None is returned.
        """
        comment_id = kwargs.get("comment_id", None)
        recommendation = self._find_rec_by_comment_id(comment_id)
        if recommendation is not None:
            return recommendation.comment.task

        task_id = kwargs.get("task_id", None)
        recommendation = self._find_rec_by_task_id(task_id)
        if recommendation is not None:
            return recommendation.comment.task

        return None

    def find_test(self, **kwargs):
        """Locate a test within this CommitEvent.

        A search is performed for a test that is co-located with the specified
        comment or task.

        Both or either a comment and task may be specified.

        Args:
            **kwargs:
                comment_id (int): Locate a test in the same recommendation
                    as the specified comment.
                task_id (int): Locate a test in the same recommendation
                    as the specified comment.
        Returns:
            If a match is found, a Test object is returned.
        """
        comment_id = kwargs.get("comment_id", None)
        recommendation = self._find_rec_by_comment_id(comment_id)
        if recommendation is not None:
            return recommendation.test

        task_id = kwargs.get("task_id", None)
        recommendation = self._find_rec_by_task_id(task_id)
        if recommendation is not None:
            return recommendation.test

        return None

    def safe_save(self, **kwargs):
        """ A save operation that does not raise exceptions.

        Args:
            kwargs (dict):
                The same parameters apply as the save() method.

        Returns:
            True (bool):
                The save operation succeeded.
            False (bool)
                The save operation failed.
        Raises:
            No exceptions are raised.
        """
        logger = kwargs.get("logger", None)
        try:
            self.save(*kwargs)
            return True
        except RedisError as e:
            if logger:
                log.error(
                    f"The CommitEvent for {self.uid} "
                    f"could not be saved: {e}."
                )
            return False

    def save(self, **kwargs):
        """ Save the data of this CommitEvent instance in the cache.

        This commit event replaces existing data at the cache key location.

        Args:
            kwargs (dict): Optional arguments.
                ex: sets an expire flag on key name for ex seconds.
                    Default is None.
                px: sets an expire flag on key name for px milliseconds.
                    Default is None.
                nx: if set to True, set the value at key name
                    to value only if it does not exist.
                    Default is False.
                xx: if set to True, set the value at key name to value only
                    Default is False.
        Raises:
           A RedisError is raised if the operation fails.
        """
        if self._test_advisor_cache is None:
            raise ValueError(
                "The bind value is missing. This object must be "
                "bound to a cache server before it can be saved."
            )

        ex = kwargs.get("ex", None)
        px = kwargs.get("px", None)
        nx = kwargs.get("nx", False)
        xx = kwargs.get("xx", False)

        # This method provides the only entry point to write to the
        # back-end cache.  This helps prevent data corruption in the
        # back-end cache. Do not provide another way to write to the
        # cache without committee approval.
        self._test_advisor_cache._redis.set(
            self.uid, json.dumps(self.json()), ex, px, nx, xx
        )
        return True

    def update_recommendation(self, recommendation_in, append=False):
        """Update a recommendation in the list of recommendations in the
        list assigned to the recommendations attribute.

        If an existing recommendation instance is passed into this method,
        the instance is updated in its parent, a CommitEvent instance.

        If the incoming recommendation instance is not in the recommendations
        list, the instance will be added to the list if the append parameter
        it True.

        If a match is not not made and the append parameter is False, an
        AttributeError is raised.

        Matching Strategy

        A recommomendation passed in as a parameter is considered to be a match
        if the comment_id of the recommendation matches a recommendation that
        is already assigned to the CommitEvent object. Care should be taken
        when adding a comment to a recommendation when the comment does not
        have a comment_id.

        If the recommendation has a comment but does not have a comment_id,
        the task_id, if any, will constitute a match. This situation is
        unlikely. It would only occur if a CommitEvent object was being
        constructed in an unusual sequence. That is, a task is typically
        associated with an existing comment which should have a comment_id
        already.

        Args:
            recommendation_in (Recommendation): A recommendation instance.
            append (bool): The append attribute, when True, updates the
                recommendations list by appending a new recommendation.
                The default value is False.
        Returns:
            The return value indicates whether of not a recommendation in
            the CommitEvent object was updated.

            bool: True, if the incoming recommendation matches an existing
                recommendation and it is updated in the CommitEvent object
                or the recommendation is appended to the recommendation list.

                False, if the recommendations in the recommendation list was
                not updated due to a non-match of a recommendation and the
                append value is False.
        Raises:
            AttributeError: If the incoming recommendation cannot be matched to
                an existing recommendation a AttributeError is raised if the
                append parameter is False.
            TypeError: If the incoming recommendation is not a Recommendation
                object.
        """
        if not isinstance(recommendation_in, Recommendation):
            raise TypeError("The parameter must be a Recommendation object.")

        # Below, attempts are made to match the incoming, updated
        # recommendation object to an existing recommendation
        # in the self.recommendations list. If a match is found, the update is
        # made in the self.recommendations list and the the rec_updated value
        # is set to True.
        rec_updated = self._update_rec_by_object(recommendation_in)

        if rec_updated is False:
            rec_updated = self._update_rec_by_comment_id(recommendation_in)

        if rec_updated is False:
            rec_updated = self._update_rec_by_recommendation_id(
                recommendation_in
            )

        if rec_updated is False:
            rec_updated = self._update_rec_by_task_id(recommendation_in)

        if rec_updated is False:
            if append is True:
                self.recommendations.append(recommendation_in)
                rec_updated = True
            else:
                raise AttributeError(
                    "Cannot update recommendation. "
                    "No matching recommendation "
                    "was found and append is False."
                )

        return rec_updated

    def _update_rec_by_comment_id(self, recommendation_in):
        rec_updated = False
        try:
            rec_idx = self.find_recommendation(
                comment_id=recommendation_in.comment.comment_id, index=True
            )
            if rec_idx is not None:
                self.recommendations[rec_idx] = recommendation_in
                rec_updated = True
        except AttributeError:
            # It may be a recommendation without a comment.
            pass

        return rec_updated

    def _update_rec_by_object(self, recommendation_in):
        num_matches = 0
        rec_updated = False
        for existing_rec in self.recommendations:
            if recommendation_in is existing_rec:
                # No action needed for the object
                # since the object itself has been updated.
                num_matches += 1
                rec_updated = True

        if num_matches > 1:
            raise AttributeError(
                "Multiple copies of the same recommendation "
                "have been assigned to this CommitEvent: "
                f"{recommendation_in}."
            )

        return rec_updated

    def _update_rec_by_recommendation_id(self, recommendation_in=None):
        rec_updated = False

        rec_idx = self.find_recommendation(
            recommendation_id=recommendation_in.recommendation_id, index=True
        )
        if rec_idx is not None:
            self.recommendations[rec_idx] = recommendation_in
            rec_updated = True

        return rec_updated

    def _update_rec_by_task_id(self, recommendation_in):
        rec_updated = False
        try:
            rec_idx = self.find_recommendation(
                task_id=recommendation_in.comment.comment_id.task_id,
                index=True,
            )
            if rec_idx is not None:
                self.recommendations[rec_idx] = recommendation_in
                rec_updated = True
        except AttributeError:
            # It may be a recommendation without a comment. Or, a comment
            # without a task.
            pass

        return rec_updated

    # This attribute is unique to CommitEvent objects and does not behave
    # like other attributes on JsonRepr objects. The value is set at
    # object initialization or at run time.
    @property
    def cache(self):
        return self._test_advisor_cache

    @cache.setter
    def cache(self, value):
        raise AttributeError("The cache attribute cannot be modified.")


class Build(JsonRepr):
    """Information about a build that produced an artifact for testing.

    Args:
        **kwargs: JSON information for a build for a recommendation
            in the Redis cache.

    Build Attributes:
        result (str): The outcome of a build.
        link (str): The URL to the build artifacts.
    """

    _attrs_std = ["link", "result"]

    link = AutoAttr()
    result = AutoAttr()

    def __init__(self, **kwargs):
        self._name = "build"
        super().__init__(**kwargs)


class Task(JsonRepr):
    """ A task associated with a comment in Bitbucket.

    Args:
        **kwargs: JSON information for a task in Bitbucket.

    Task Attributes:
        task_id (int): The identifier used by Bitbucket to locate the task.
            The task_id attribute becomes immutable once a value is assigned.
        task_data (str): Details about the task.
        task_status (str): The current state for the task.

    Raises:
        AttributeError: When an attempt is made to modify the task_id after
            a value has been assigned to the attribute.
    """

    _attrs_std = ["task_id", "task_data", "task_status"]

    task_id = SetOnceAttr(int)
    task_data = AutoAttr()
    task_status = AutoAttr()

    def __init__(self, **kwargs):
        self._name = "task"
        super().__init__(**kwargs)


class Comment(JsonRepr):
    """Information about a comment and (optional) associated task attached to
    the comment.

    Args:
        **kwargs: JSON information for a build for a Bitbucket comment.

    Comment Attributes:
        comment_id (int): The identifier assigned by Bitbucket.

            In the Test Advisor application, the value of the comment_id
            can vary through the lifecycle of the comment.

            The commit_id attribute becomes immutable
            once a value is assigned.

            The v
        advice (str): Verbiage entered into the body of the comment posted
            to a Bitbucket pull request.
        task (Task): The Task object represents a task that is associated with
            the Bitbucket comment.

    Raises:
        AttributeError: When an attempt is made to modify the commit_id after
            a value has been assigned to the attribute.
    """

    _attrs_std = ["advice", "comment_id"]
    _attrs_json = ["task"]

    advice = AutoAttr()
    comment_id = SetOnceAttr(int)
    task = AutoAttr(Task)

    def __init__(self, **kwargs):
        self._name = "comment"
        super().__init__(**kwargs)


class Recommendation(JsonRepr):
    """This object reflects output from Test Advisor for a specific commit.

    The recommendation always contains a comment posted to a Bitbucket
    pull request. Depending on Test Advisor assessment, the recommendation
    may have task, build, and test elements.

    Args:
        **kwargs: JSON information for a recommendation for an entry
            in the Redis cache.

    Attributes:

        comment (Comment): The Bitbucket comment that will be
            or has been posted to a pull request (PR).
        auto_test_run (JSON): If the test should start automatically.
            This attribute is used to indicate that a recommended test
            was determined by Test Advisor is mandatory. The attribute remains
            unchanged even after the test was run.

            If this object is created and the auto_test_run has not been set,
            the value is None. The auto_test_run valuse should be tested
            with 'is' for True, False, and None.

        test (Test): Test information.
        build (JSON): The Build object contains information about a build that
            produced an artifact for testing.
    """

    _attrs_std = ["auto_test_run", "recommendation_id"]
    _attrs_json = ["build", "comment", "test"]

    auto_test_run = AutoAttr(bool)
    build = AutoAttr(Build)
    comment = AutoAttr(Comment)
    test = AutoAttr(Test)

    def __init__(self, **kwargs):
        self._name = "recommendation"

        # Import or create a uuid.
        kwargs["recommendation_id"] = kwargs.get(
            "recommendation_id", str(uuid4())
        )
        # Initialize the recommendation_id with a (required) value
        # and other attributes that may or may not be assigned values.
        super().__init__(**kwargs)


class TACacheFactory:
    """This factory creates objects using classes in the testadvisorcache
    module.

    Args:
        class_name (str): The name of a class in the testadvisorcache module.

        **data (dict): A reference to a dictionary containing values with
            which to initialize the object with. This parameter is optional.
            If data is not supplied, an 'empty' object is returned.

            The data dictionary can be a simple JSON object or a Python
            dictionary containing testadvisorcache objects.

            For example, both of the following data sets are valid input
            for a comment object created by the factory class:
                comment = TACacheFactory.make('comment', **data)

                data = {
                    "comment": {
                        "comment_id": 2001,
                        "advice": "advice_2",
                        "task": {
                            "task_id": 2002,
                            "task_data": "task.task_data_2",
                            "task_status": "task.task_status_2"
                        }
                    }
                }

                data = {
                    "comment": {
                        "comment_id": 2001,
                        "advice": "advice_2",
                        "task": <class 'testadvisor.testadvisorcache.Task'>
                    }
                }

    Returns:
        An object from the testadvisorcache module.
    """

    _simple_classes = ["build", "task", "test"]
    _tac_classes = {
        "build": Build,
        "comment": Comment,
        "commitevent": CommitEvent,
        "recommendation": Recommendation,
        "task": Task,
        "test": Test,
    }

    @staticmethod
    def make(class_name, **data):
        class_name = class_name.lower()
        if class_name not in TACacheFactory._tac_classes:
            raise TypeError(
                f"No such class: {class_name}. The TACacheFactory "
                "cannot make a {class_name} object."
            )

        if class_name in TACacheFactory._simple_classes:
            return TACacheFactory._make_simple_obj(class_name, **data)
        else:
            return getattr(TACacheFactory, f"_make_{class_name}")(**data)

    @staticmethod
    def supported_classes():
        return TACacheFactory._tac_classes

    @staticmethod
    def _make_comment(**data):
        comment_data = data.copy()
        task = comment_data.get("task", None)
        if task and not isinstance(task, Task):
            comment_data["task"] = TACacheFactory._make_simple_obj(
                "task", **task
            )
        return TACacheFactory._tac_classes["comment"](**comment_data)

    @staticmethod
    def _make_commitevent(**data):
        ce_data = data.copy()
        rec_candidates = ce_data.get("recommendations", [])
        rec_objects = []

        for rec_data in rec_candidates:
            if rec_data and not isinstance(rec_data, Recommendation):
                rec_objects.append(
                    TACacheFactory._make_recommendation(**rec_data)
                )
        if rec_objects:
            ce_data["recommendations"] = rec_objects

        return TACacheFactory._tac_classes["commitevent"](**ce_data)

    @staticmethod
    def _make_recommendation(**data):
        rec_data = data.copy()
        buf = rec_data.get("comment", None)
        if buf and not isinstance(buf, Comment):
            rec_data["comment"] = TACacheFactory._make_comment(**buf)

        for class_name in "build", "test":
            buf = rec_data.get(class_name, None)
            if not buf and (
                # The reference object in isinstance cannot be parameterized,
                # e.g. isinstance(buf, classes['class_name']) is invalid.
                not isinstance(buf, Build)
                or not isinstance(buf, Test)
            ):
                continue

            rec_data[class_name] = TACacheFactory._make_simple_obj(
                class_name, **buf
            )

        return TACacheFactory._tac_classes["recommendation"](**rec_data)

    @staticmethod
    def _make_simple_obj(class_name, **data):
        return TACacheFactory._tac_classes[class_name](**data)
