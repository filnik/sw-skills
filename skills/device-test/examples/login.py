"""Example scenario: signing in, with a wrong and a right password.

Copy it into your project (device-tests/login.py) and change the tags to your app's. Run it from the project:

    devtest --owner me --device pixel run device-tests/login.py
    devtest --owner me --platform both run device-tests/login.py     # Android and iPhone side by side

The test account comes from the environment, never from the file: LOGIN_USER and LOGIN_PASSWORD.
"""

import os

from devtest_common import Blocked
from devtest_scenario import Scenario

S = Scenario("LOGIN-1 sign in", criteria={
    "AC1": "a wrong password shows an error and stays on the login screen, without logging a session",
    "AC2": "the right password opens the home screen and logs 'session started'",
    "AC3": "the signed-in user is stored in the app prefs",
    "AC4": "a fingerprint prompt is offered after the first sign-in",
})


def account():
    user, password = os.environ.get("LOGIN_USER"), os.environ.get("LOGIN_PASSWORD")
    if not user or not password:
        raise Blocked("set LOGIN_USER and LOGIN_PASSWORD to a test account")
    return user, password


@S.pre
def signed_out(d):
    # Idempotent: whatever the last run left, start signed out on the login screen.
    d.launch(restart=True)
    if d.dev.visible("screen.home", timeout=5):
        d.tap("home.sign_out")
    d.expect_screen("screen.login")


@S.step("A wrong password is refused", covers=["AC1"])
def _(d):
    user, _ = account()
    m = d.mark()
    d.set_text("login.email", user)
    d.set_text("login.password", "not-the-password")
    d.tap("login.submit")
    d.expect_visible("text~wrong password")
    d.expect_screen("screen.login")
    # An absence counts only because step 2 asserts the same line present.
    d.expect_no_log(r"session started", since=m)


@S.step("The right password opens home", covers=["AC2", "AC3"])
def _(d):
    user, password = account()
    m = d.mark()
    d.set_text("login.password", password)
    d.tap("login.submit")
    d.expect_screen("screen.home", timeout=20)
    d.expect_log(r"session started", since=m)
    if d.platform == "ios":
        # Kept in the Keychain on iOS, which the tool cannot read: reported N/A, never PASS.
        d.unverified("AC3", "the iOS app keeps the user in the Keychain")
    else:
        d.expect_pref("signed_in_user", equals=user)


S.skip("AC4", "the test phones have no enrolled fingerprint")


@S.post
def restore(d):
    # Always runs, pass or fail: leave the app signed out for the next scenario.
    if d.dev.visible("screen.home"):
        d.tap("home.sign_out")
