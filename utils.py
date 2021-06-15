import logging


def getmylogger(name):
    # Create a custom logger
    logger = logging.getLogger(name)
    # Create handler
    f_handler = logging.FileHandler('client.log')
    f_handler.setLevel(logging.DEBUG)
    # Create formatter and add it to handler 
    f_format = logging.Formatter("%(levelname)s : %(asctime)s (%(threadName)-2s) %(message)s")
    f_handler.setFormatter(f_format)
    # Add handler to the logger
    logger.addHandler(f_handler)
    logger.setLevel(logging.DEBUG)
    return logger


# Create a custom enum generator for further usage
def enum(**enums):
    return type('Enum', (), enums)

