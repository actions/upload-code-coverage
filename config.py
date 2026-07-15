from pathlib import Path
from user_error import UserError

class Config:
    def __init__(self, env: dict[str, str]):
        self.file_path = env.get("INPUT_FILE")
        self.fail_on_error = env.get("FAIL_ON_ERROR", "true").lower() != "false"
        self.commit_oid = env.get("COMMIT_OID")
        self.ref = env.get("REF", "")
        self.pr_number = env.get("PR_NUMBER", "")
        self.language = env.get("INPUT_LANGUAGE")
        self.label = env.get("INPUT_LABEL")

        self.validate()

    def validate(self) -> None:
        if not self.file_path or not Path(self.file_path).is_file():
            raise UserError(f"Coverage file not found: {self.file_path}", "file_not_found")
        if not self.commit_oid:
            raise UserError("Commit OID is required", "missing_commit_oid")
        if not self.language:
            raise UserError("Language is required", "missing_language")
        if not self.label:
            raise UserError("Label is required", "missing_label")

    def log_upload_parameters(self) -> None:
        file_size = Path(self.file_path).stat().st_size
        print("::group::Upload parameters")
        print(f"  commit_oid: {self.commit_oid}")
        print(f"  ref: {self.ref or '<not set>'}")
        print(f"  pr_number: {self.pr_number or '<not set>'}")
        print(f"  language: {self.language}")
        print(f"  label: {self.label}")
        print(f"  file: {self.file_path} ({file_size} bytes)")
        print("::endgroup::")