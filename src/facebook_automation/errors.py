class PublishError(Exception):
    def __init__(self, code: str, message: str, exit_code: int = 2):
        super().__init__(message)
        self.code, self.message, self.exit_code = code, message, exit_code

    def json(self):
        return {"code": self.code, "message": self.message}
