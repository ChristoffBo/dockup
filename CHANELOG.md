## latest (2026-01-26)
Fixed a split editor Parse issue
## latest (2026-01-18)
added shell to the ui, you can now shell into your containers, also added the ability to set per container in the stack's updates, be it auto update or check only, be aware that i could not get the shell to work in firefox, tested and working in brave and chromium.
## latest (2026-01-16)
Updated Dependencies and changed a spilt editor parsing issue with using local docker registries. it will now support ip addressea if you wish to go the http route.
## latest (2026-01-12)
Updated where DockUp checks for updates. it used to check the image of the container it now checks the compsoe files. there might be a breaking change where by a stack might get stuck on update. just delete if the stack image and up it again.
## latest (2025-12-18)
fixed an issue with the docker run parser
## latest (2025-12-17)
performance update, should see around 80 percent less API calls to docker.
## latest (2025-12-16)
Added Appdata size per Stack in UI, had a size issue, this will help to faultfind, see updated compose in readme.
## latest (2025-12-14)
Dockup completed will only get dependency updates for now..
## latest (2025-12-14)
DockUp Wil now auto insert your root appdata, can be set in settings
## latest (2025-12-3)
small UI revamp, added docker and host polling into the settings.
## latest (2025-12-3)
added cve scans.
## latest (2025-12-2)
squashed more bugs. added dockerhub rate limit on dashboard.
## latest (2025-11-29)
Added Peer mode, can now connect two dockups in peer mode.
## latest (2025-11-28)
squashed some bugs upgraded dependencies
## latest (2025-11-27)
added auth, can be switched on in settings and alternatively first run.
## latest (2025-11-26)
added a network tab, can now create networks and delete them from the UI. hope I did not break anything.
## latest (2025-11-25)
added advanced split editor functions.
fixed a nasty duplicate compose bug
## latest (2025-11-25)
Added clone stack button. fixed split editor parse issue
added stack tagging
## latest (2025-11-23)
Added Network stats, added port detection and fxied stop container.

## latest (2025-11-19)
Version 1 released.
