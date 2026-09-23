"""Bounded application login smoke test; prints fixed error categories only."""
import asyncio
import traceback

from .browser import BrowserLogin, Profiles, failure_category
from .config import Config


async def main():
    config = Config.from_env()
    login = BrowserLogin(Profiles(config.profiles, config.backend_origin))
    target = {"provider": "gradescope", "base_url": "https://www.gradescope.com",
              "credential_id": "gradescope-launch-diagnostic"}
    try:
        async with asyncio.timeout(15):
            cookies = await login.run(target, asyncio.Event())
            cookies.clear()
            print("diagnostic=authenticated", flush=True)
    except TimeoutError:
        print("diagnostic=login_wait_timeout", flush=True)
    except Exception as error:
        print("diagnostic_type=" + type(error).__name__, flush=True)
        print("diagnostic_frames=" + ",".join(frame.name + ":" + str(frame.lineno)
              for frame in traceback.extract_tb(error.__traceback__)), flush=True)
        print("diagnostic=" + failure_category(error), flush=True)
        # Fixed diagnostic vocabulary, not arbitrary exception content.
        vocabulary = ("launch_persistent_context", "goto", "cookies", "enoent", "eacces", "eperm",
                      "expected", "argument", "unsupported", "executable", "profile", "sandbox",
                      "closed", "spawn", "directory", "mkdtemp", "channel", "invalid", "path",
                      "not found", "too long", "missing", "user_data_dir", "permission",
                      "not supported", "object", "boolean", "string", "number", "list")
        message = str(error).lower()
        print("diagnostic_markers=" + ",".join(word for word in vocabulary if word in message), flush=True)


if __name__ == "__main__":
    asyncio.run(main())
