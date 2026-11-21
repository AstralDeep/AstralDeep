"""Defines the one error type every node pack builder module raises and assemble.py's command line reports.
It lives in its own module so assemble.py run as a script and the natives builder agree on the class.
"""


class PackError(Exception):
    pass
