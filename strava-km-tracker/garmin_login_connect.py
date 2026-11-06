import json
from getpass import getpass
from pathlib import Path

from garminconnect import Garmin


def main():
    email = input("Garmin email: ").strip()
    password = getpass("Garmin password: ")
    tokenstore = Path.home() / ".garminconnect"

    client = Garmin(
        email,
        password,
        prompt_mfa=lambda: input("MFA code: ").strip(),
    )
    client.login(str(tokenstore))

    token_file = tokenstore / "garmin_tokens.json"
    with token_file.open(encoding="utf-8") as source:
        tokens = json.load(source)
    export_file = Path.cwd() / "garmin_tokens_export.json"
    with export_file.open("w", encoding="utf-8") as destination:
        json.dump({"garmin_tokens.json": tokens}, destination)

    print(f"Login successful. Token export saved to: {export_file}")
    print(f"Account: {client.get_full_name()}")


if __name__ == "__main__":
    main()