# Ensure background poller starts inside the worker AFTER fork.
def post_fork(server, worker):
    from app import start_poller
    start_poller()
