# A module that takes 1.5 s to import: SIGTERM arrives while it is loading.
import time

time.sleep(1.5)


def handler(event, context):
    return "slow"
