This code provides a single access point to a Redis cache used for \
a test automation application. The purpose of this interface is two-fold.

One purpose is to minimize the risk of data corruption in the cache. \
Using only this interface eliminates the need for various modules \
or scripts to include code that writes to the Redis cache. As each \
implementation increases the risk that a mistake will be made that \
could be made that corrupts data in the cache.

The other purpose of this script is for maintenance purposes. \
Encapsulating http requests and subsequent parsing of the \
returned data in this module eliminates the need for messy \
and duplicate code in the Test Advisor application. \
This also results in a single point of edit in the event that \
the Redis schema is updated. The Python objects in this module \
represent logical encapsualtions of data that also help simplify the code \
in the scripts that consume this code.

### TODO: 
While this module works as expected, the Python code could be cleaned up. \
The cleanup would not affect performance but would make the code easier to \
maintain and, frankly, more Pythonic by being "prettier".

1) Consolidate all attibute decorators to a single factory class, e,g `CustomAttr`. \
The factory class creates an attibute as specified in the declaration \
as a parameter rather than explicitly specifying the attribute class.


    current syntax:

    ```python
    commit = SetOnceAttr(str)
    event_time = DatetimeAttr()
    pr_num = SetOnceAttr(int)
    recommendations = AutoAttr(list)
    ```

    becomes:

    ```python
    commit = CustomAttr(data_type=str)
    event_time = CustomAttr(data_type=datetime)
    pr_num = CustomAttr(data_type=int, mutable=False)
    recommendations = CustomAttr(data_type=list)
    ```

2) Move the manual tracking of display characteristics from a list
maintenance task to a declaration task. The display characteristics are
handled in the decorator implemenataions.

    current syntax:
    ```python
    class Recommendation(JsonRepr):
        _attrs_std = ["auto_test_run", "recommendation_id"]
        _attrs_json = ["build", "comment", "test"]

        recommendation_id = AutoAttr(int)
        auto_test_run = AutoAttr(bool)
        build = AutoAttr(Build)
        comment = AutoAttr(Comment)
        test = AutoAttr(Test)    
    ```

    becomes:
    ```python
    class CustomAttr:
        # Lists updated during attribute initializations.
        _attrs_std = []
        _attrs_json = []

    class Recommendation(JsonRepr):
        recommendation_id = CustomAttr(data_type=int, display=std)
        auto_test_run = CustomAttr(data_type=bool, display=std)
        build = CustomAttr(data_type=Build, display=json)
        comment = CustomAttr(data_type=Comment, display=json)
        test = CustomAttr(data_type=Test, display=json)
    ```