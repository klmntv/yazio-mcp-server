"""Constants shared by the MCP server.

The OAuth client credentials are the ones the YAZIO mobile app ships with; they
were read back out of the mitmproxy capture the spec was generated from. They
identify the *app*, not the user — every install sends the same pair — so they
are not a secret and belong in source rather than in the environment. The user's
own credentials never appear here; they arrive per request (see auth.py).
"""

BASE_URL = "https://yzapi.yazio.com"
APP_TIMEZONE = "Europe/Moscow"

CLIENT_ID = "3_5rbw4kehpugw8ogsc8ck8oo4ogswgckcskc04gcg8kk8k48ssw"
CLIENT_SECRET = "25gdtt1hvdi8gwowoww4oo88sgsw0oo04o0og0kkgwwks8k0k"

# The API refuses any request whose client version it does not recognise,
# answering 403 `{"error":"version_blocked"}` — and the version is carried in the
# User-Agent rather than in a header of its own. This is the string the captured
# app sent; it will need bumping whenever YAZIO retires the version.
USER_AGENT = "YAZIO/26.30.1 (com.yazio.ios.YAZIO; build:2607271240; iOS 27.0.0) Ktor"

# Product search is ranked by language, so the header decides whether a German
# user searching "Quark" gets German results.
ACCEPT_LANGUAGE = "de-DE,de;q=0.9"

DEFAULT_HEADERS = {
    "user-agent": USER_AGENT,
    "accept-language": ACCEPT_LANGUAGE,
    "accept": "*/*",
}

# Requests time out a little sooner than the spec's own 30s budget so that a
# stalled upstream call surfaces as a tool error rather than as a dead session.
HTTP_TIMEOUT = 20.0

# The four meal slots YAZIO buckets consumed items into. The API accepts these
# verbatim as the `daytime` field.
DAYTIMES = ("breakfast", "lunch", "dinner", "snack")
