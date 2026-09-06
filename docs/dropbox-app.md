# Dropbox access

Only **sending saves back to Delta** needs Dropbox access. Pulling from Delta —
saves, ROMs and cheats coming *to* RetroArch — reads the folder the Dropbox
desktop client already keeps on disk and needs no account access at all.

## Why it is needed for sending

Delta downloads the *exact* Dropbox revision that a save record names. That
revision is assigned by Dropbox when the desktop client uploads, and it is not
visible in the local folder — so the tool has to read it back before it can
write a record Delta will follow. Getting this wrong is not subtle: an earlier
version guessed, and Delta failed to sync until the record was repaired by hand.

That is the whole reason for the permission. The scope requested is
`files.metadata.read` and nothing else:

| | |
| --- | --- |
| Read file metadata (names, sizes, revisions) | Yes — this is the point |
| Read your file contents | No |
| Upload or delete anything | No |
| Touch file properties | No |

The tool never uploads through the API. The Dropbox desktop client already does
that; doing it twice would race with it.

## Using the bundled app

Nothing to do. Press **Authorise Dropbox…** in the launcher, approve in the
browser, paste the code back. The bundled app key is
`j817l20q0w1nfkd`.

An app key is a public identifier, not a credential. It is in this repository
deliberately: [PKCE](https://datatracker.ietf.org/doc/html/rfc7636) exists so a
desktop app can authenticate without holding a secret, which is why this flow
never uses the app *secret* and never should. Knowing the key gets you nothing —
authorisation still happens in your own browser, against your own account.

## Using your own app instead

Worth doing if you would rather not authorise against someone else's app
registration, or if the bundled one hits its user limit (see below).

1. Go to <https://www.dropbox.com/developers/apps> and choose **Create app**
2. **Scoped access** → **Full Dropbox** → give it any name
3. On the **Permissions** tab, tick **`files.metadata.read`**, then **Submit**
   — this step is easy to miss, and authorisation will appear to work without
   it right up until the first API call fails
4. Copy the **App key** from the **Settings** tab. Not the App secret; that is
   never needed here and should not be shared
5. In the launcher: **Use own app key…**, paste it, then **Authorise Dropbox…**
   again

Clearing that field restores the bundled key. It is also settable by hand as
`dropbox_app_key` under `[options]` in `config.toml`.

## The user limit on the bundled app

Dropbox apps start in *development status*. Such an app can link up to 50 users
before it must be approved for production; at 50, there is a two-week window to
apply before further users are blocked. Existing users keep working either way.

So if this tool ever gets popular, new users may see authorisation refused until
the app is approved, and registering your own is the immediate workaround.

Production review checks that an app uses API v2 and requests only the
permissions it needs — both of which hold here, since the entire API surface
used is `files/get_metadata` and `files/list_folder` under a single read-only
scope.

## Revoking access

<https://www.dropbox.com/account/connected_apps> — disconnect the app there. To
also remove the stored token from this machine, delete `dropbox-token.json` from
the tool's folder. Pulling from Delta keeps working without it.
