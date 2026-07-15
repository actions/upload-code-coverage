class UserError(Exception):
    """Exception class for failures caused by the user."""
    def __init__(self, message: str, type: str ):
        super().__init__(message)
        self.type = type
