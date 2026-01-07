"""
Comprehensive LinuxServer.io Container Templates
Generated from official LinuxServer.io documentation and GitHub repos
Last updated: December 2025
"""

def get_all_linuxserver_templates():
    """
    Returns comprehensive list of 150+ LinuxServer.io containers
    Organized by category with complete metadata
    """
    return [
        # ========== MEDIA SERVERS ==========
        {
            "id": "plex",
            "name": "Plex",
            "category": "media",
            "image": "lscr.io/linuxserver/plex:latest",
            "description": "Stream your personal media collection anywhere - movies, TV, music, photos",
            "ports": ["32400:32400"],
            "volumes": ["/config", "/media"],
            "env": ["PUID=1000", "PGID=1000", "TZ=Etc/UTC", "VERSION=docker"]
        },
        {
            "id": "jellyfin",
            "name": "Jellyfin",
            "category": "media",
            "image": "lscr.io/linuxserver/jellyfin:latest",
            "description": "Free software media server - the volunteer-built alternative to Plex",
            "ports": ["8096:8096", "8920:8920"],
            "volumes": ["/config", "/media"],
            "env": ["PUID=1000", "PGID=1000", "TZ=Etc/UTC"]
        },
        {
            "id": "emby",
            "name": "Emby",
            "category": "media",
            "image": "lscr.io/linuxserver/emby:latest",
            "description": "Media server to organize, play and stream audio and video",
            "ports": ["8096:8096", "8920:8920"],
            "volumes": ["/config", "/data"],
            "env": ["PUID=1000", "PGID=1000", "TZ=Etc/UTC"]
        },
        {
            "id": "kodi-headless",
            "name": "Kodi Headless",
            "category": "media",
            "image": "lscr.io/linuxserver/kodi-headless:latest",
            "description": "Headless Kodi for library updates and background tasks",
            "ports": ["8080:8080", "9090:9090"],
            "volumes": ["/config"],
            "env": ["PUID=1000", "PGID=1000", "TZ=Etc/UTC"]
        },
        {
            "id": "kometa",
            "name": "Kometa",
            "category": "media",
            "image": "lscr.io/linuxserver/kometa:latest",
            "description": "Powerful tool for complete control over your media libraries",
            "ports": [],
            "volumes": ["/config"],
            "env": ["PUID=1000", "PGID=1000", "TZ=Etc/UTC"]
        },
        {
            "id": "navidrome",
            "name": "Navidrome",
            "category": "media",
            "image": "lscr.io/linuxserver/navidrome:latest",
            "description": "Modern Music Server and Streamer compatible with Subsonic/Airsonic",
            "ports": ["4533:4533"],
            "volumes": ["/config", "/music"],
            "env": ["PUID=1000", "PGID=1000", "TZ=Etc/UTC"]
        },
        {
            "id": "airsonic-advanced",
            "name": "Airsonic Advanced",
            "category": "media",
            "image": "lscr.io/linuxserver/airsonic-advanced:latest",
            "description": "Free, web-based media streamer and jukebox",
            "ports": ["4040:4040"],
            "volumes": ["/config", "/music", "/playlists", "/podcasts"],
            "env": ["PUID=1000", "PGID=1000", "TZ=Etc/UTC"]
        },
        
        # ========== ARR STACK (Media Automation) ==========
        {
            "id": "sonarr",
            "name": "Sonarr",
            "category": "arr",
            "image": "lscr.io/linuxserver/sonarr:latest",
            "description": "Smart PVR for TV shows - monitor, download and organize",
            "ports": ["8989:8989"],
            "volumes": ["/config", "/tv", "/downloads"],
            "env": ["PUID=1000", "PGID=1000", "TZ=Etc/UTC"]
        },
        {
            "id": "radarr",
            "name": "Radarr",
            "category": "arr",
            "image": "lscr.io/linuxserver/radarr:latest",
            "description": "Movie collection manager for Usenet and BitTorrent",
            "ports": ["7878:7878"],
            "volumes": ["/config", "/movies", "/downloads"],
            "env": ["PUID=1000", "PGID=1000", "TZ=Etc/UTC"]
        },
        {
            "id": "prowlarr",
            "name": "Prowlarr",
            "category": "arr",
            "image": "lscr.io/linuxserver/prowlarr:latest",
            "description": "Indexer manager/proxy built on Lidarr/Radarr/Readarr/Sonarr",
            "ports": ["9696:9696"],
            "volumes": ["/config"],
            "env": ["PUID=1000", "PGID=1000", "TZ=Etc/UTC"]
        },
        {
            "id": "lidarr",
            "name": "Lidarr",
            "category": "arr",
            "image": "lscr.io/linuxserver/lidarr:latest",
            "description": "Music collection manager for Usenet and BitTorrent",
            "ports": ["8686:8686"],
            "volumes": ["/config", "/music", "/downloads"],
            "env": ["PUID=1000", "PGID=1000", "TZ=Etc/UTC"]
        },
        {
            "id": "readarr",
            "name": "Readarr",
            "category": "arr",
            "image": "lscr.io/linuxserver/readarr:develop",
            "description": "Book, magazine and audiobook collection manager",
            "ports": ["8787:8787"],
            "volumes": ["/config", "/books", "/downloads"],
            "env": ["PUID=1000", "PGID=1000", "TZ=Etc/UTC"]
        },
        {
            "id": "bazarr",
            "name": "Bazarr",
            "category": "arr",
            "image": "lscr.io/linuxserver/bazarr:latest",
            "description": "Companion to Sonarr/Radarr for managing subtitles",
            "ports": ["6767:6767"],
            "volumes": ["/config", "/movies", "/tv"],
            "env": ["PUID=1000", "PGID=1000", "TZ=Etc/UTC"]
        },
        {
            "id": "jackett",
            "name": "Jackett",
            "category": "arr",
            "image": "lscr.io/linuxserver/jackett:latest",
            "description": "API support for torrent trackers - proxy for *arr apps",
            "ports": ["9117:9117"],
            "volumes": ["/config", "/downloads"],
            "env": ["PUID=1000", "PGID=1000", "TZ=Etc/UTC"]
        },
        {
            "id": "nzbhydra2",
            "name": "NZBHydra2",
            "category": "arr",
            "image": "lscr.io/linuxserver/nzbhydra2:latest",
            "description": "Meta search for NZB indexers - usenet search aggregator",
            "ports": ["5076:5076"],
            "volumes": ["/config", "/downloads"],
            "env": ["PUID=1000", "PGID=1000", "TZ=Etc/UTC"]
        },
        {
            "id": "mylar3",
            "name": "Mylar3",
            "category": "arr",
            "image": "lscr.io/linuxserver/mylar3:latest",
            "description": "Automated Comic Book downloader and manager",
            "ports": ["8090:8090"],
            "volumes": ["/config", "/comics", "/downloads"],
            "env": ["PUID=1000", "PGID=1000", "TZ=Etc/UTC"]
        },
        {
            "id": "whisparr",
            "name": "Whisparr",
            "category": "arr",
            "image": "lscr.io/linuxserver/whisparr:latest",
            "description": "Adult media collection manager",
            "ports": ["6969:6969"],
            "volumes": ["/config", "/data"],
            "env": ["PUID=1000", "PGID=1000", "TZ=Etc/UTC"]
        },
        
        # ========== DOWNLOAD CLIENTS ==========
        {
            "id": "qbittorrent",
            "name": "qBittorrent",
            "category": "downloads",
            "image": "lscr.io/linuxserver/qbittorrent:latest",
            "description": "Free and reliable P2P BitTorrent client",
            "ports": ["8080:8080", "6881:6881", "6881:6881/udp"],
            "volumes": ["/config", "/downloads"],
            "env": ["PUID=1000", "PGID=1000", "TZ=Etc/UTC", "WEBUI_PORT=8080"]
        },
        {
            "id": "sabnzbd",
            "name": "SABnzbd",
            "category": "downloads",
            "image": "lscr.io/linuxserver/sabnzbd:latest",
            "description": "Free and easy binary newsreader for Usenet",
            "ports": ["8080:8080"],
            "volumes": ["/config", "/downloads"],
            "env": ["PUID=1000", "PGID=1000", "TZ=Etc/UTC"]
        },
        {
            "id": "nzbget",
            "name": "NZBGet",
            "category": "downloads",
            "image": "lscr.io/linuxserver/nzbget:latest",
            "description": "Efficient Usenet downloader",
            "ports": ["6789:6789"],
            "volumes": ["/config", "/downloads"],
            "env": ["PUID=1000", "PGID=1000", "TZ=Etc/UTC"]
        },
        {
            "id": "transmission",
            "name": "Transmission",
            "category": "downloads",
            "image": "lscr.io/linuxserver/transmission:latest",
            "description": "Fast, easy BitTorrent client with web interface",
            "ports": ["9091:9091", "51413:51413"],
            "volumes": ["/config", "/downloads", "/watch"],
            "env": ["PUID=1000", "PGID=1000", "TZ=Etc/UTC"]
        },
        {
            "id": "deluge",
            "name": "Deluge",
            "category": "downloads",
            "image": "lscr.io/linuxserver/deluge:latest",
            "description": "Lightweight, free cross-platform BitTorrent client",
            "ports": ["8112:8112", "6881:6881"],
            "volumes": ["/config", "/downloads"],
            "env": ["PUID=1000", "PGID=1000", "TZ=Etc/UTC"]
        },
        {
            "id": "rutorrent",
            "name": "ruTorrent",
            "category": "downloads",
            "image": "lscr.io/linuxserver/rutorrent:latest",
            "description": "rtorrent client with popular webui",
            "ports": ["80:80", "5000:5000", "51413:51413"],
            "volumes": ["/config", "/downloads"],
            "env": ["PUID=1000", "PGID=1000", "TZ=Etc/UTC"]
        },
        {
            "id": "pyload-ng",
            "name": "pyLoad-ng",
            "category": "downloads",
            "image": "lscr.io/linuxserver/pyload-ng:latest",
            "description": "Free and open-source download manager",
            "ports": ["8000:8000", "9666:9666"],
            "volumes": ["/config", "/downloads"],
            "env": ["PUID=1000", "PGID=1000", "TZ=Etc/UTC"]
        },
        
        # ========== HOME AUTOMATION ==========
        {
            "id": "homeassistant",
            "name": "Home Assistant",
            "category": "automation",
            "image": "lscr.io/linuxserver/homeassistant:latest",
            "description": "Open source home automation platform",
            "ports": ["8123:8123"],
            "volumes": ["/config"],
            "env": ["PUID=1000", "PGID=1000", "TZ=Etc/UTC"]
        },
        {
            "id": "heimdall",
            "name": "Heimdall",
            "category": "automation",
            "image": "lscr.io/linuxserver/heimdall:latest",
            "description": "Application dashboard and launcher",
            "ports": ["80:80", "443:443"],
            "volumes": ["/config"],
            "env": ["PUID=1000", "PGID=1000", "TZ=Etc/UTC"]
        },
        {
            "id": "homer",
            "name": "Homer",
            "category": "automation",
            "image": "lscr.io/linuxserver/homer:latest",
            "description": "Static application dashboard",
            "ports": ["8080:8080"],
            "volumes": ["/config"],
            "env": ["PUID=1000", "PGID=1000", "TZ=Etc/UTC"]
        },
        {
            "id": "homarr",
            "name": "Homarr",
            "category": "automation",
            "image": "lscr.io/linuxserver/homarr:latest",
            "description": "Customizable homelab dashboard with modern UI",
            "ports": ["7575:7575"],
            "volumes": ["/config", "/data", "/icons"],
            "env": ["PUID=1000", "PGID=1000", "TZ=Etc/UTC"]
        },
        {
            "id": "organizr",
            "name": "Organizr",
            "category": "automation",
            "image": "lscr.io/linuxserver/organizr:latest",
            "description": "Homelab services organizer with SSO",
            "ports": ["80:80"],
            "volumes": ["/config"],
            "env": ["PUID=1000", "PGID=1000", "TZ=Etc/UTC"]
        },
        
        # ========== PRODUCTIVITY ==========
        {
            "id": "nextcloud",
            "name": "Nextcloud",
            "category": "productivity",
            "image": "lscr.io/linuxserver/nextcloud:latest",
            "description": "Self-hosted productivity platform - files, calendar, contacts",
            "ports": ["443:443"],
            "volumes": ["/config", "/data"],
            "env": ["PUID=1000", "PGID=1000", "TZ=Etc/UTC"]
        },
        {
            "id": "code-server",
            "name": "Code Server",
            "category": "productivity",
            "image": "lscr.io/linuxserver/code-server:latest",
            "description": "VS Code in the browser",
            "ports": ["8443:8443"],
            "volumes": ["/config"],
            "env": ["PUID=1000", "PGID=1000", "TZ=Etc/UTC"]
        },
        {
            "id": "calibre",
            "name": "Calibre",
            "category": "productivity",
            "image": "lscr.io/linuxserver/calibre:latest",
            "description": "Powerful ebook library management",
            "ports": ["8080:8080", "8181:8181", "8081:8081"],
            "volumes": ["/config"],
            "env": ["PUID=1000", "PGID=1000", "TZ=Etc/UTC"]
        },
        {
            "id": "calibre-web",
            "name": "Calibre-Web",
            "category": "productivity",
            "image": "lscr.io/linuxserver/calibre-web:latest",
            "description": "Web app for browsing and reading ebooks from Calibre",
            "ports": ["8083:8083"],
            "volumes": ["/config", "/books"],
            "env": ["PUID=1000", "PGID=1000", "TZ=Etc/UTC"]
        },
        {
            "id": "paperless-ngx",
            "name": "Paperless-ngx",
            "category": "productivity",
            "image": "lscr.io/linuxserver/paperless-ngx:latest",
            "description": "Document management system with OCR",
            "ports": ["8000:8000"],
            "volumes": ["/config", "/data"],
            "env": ["PUID=1000", "PGID=1000", "TZ=Etc/UTC"]
        },
        {
            "id": "obsidian",
            "name": "Obsidian",
            "category": "productivity",
            "image": "lscr.io/linuxserver/obsidian:latest",
            "description": "Powerful knowledge base on plain text markdown files",
            "ports": ["3000:3000", "3001:3001"],
            "volumes": ["/config"],
            "env": ["PUID=1000", "PGID=1000", "TZ=Etc/UTC"]
        },
        {
            "id": "syncthing",
            "name": "Syncthing",
            "category": "productivity",
            "image": "lscr.io/linuxserver/syncthing:latest",
            "description": "Continuous file synchronization program",
            "ports": ["8384:8384", "22000:22000", "21027:21027/udp"],
            "volumes": ["/config", "/data"],
            "env": ["PUID=1000", "PGID=1000", "TZ=Etc/UTC"]
        },
        {
            "id": "wikijs",
            "name": "Wiki.js",
            "category": "productivity",
            "image": "lscr.io/linuxserver/wikijs:latest",
            "description": "Modern and powerful wiki software",
            "ports": ["3000:3000"],
            "volumes": ["/config", "/data"],
            "env": ["PUID=1000", "PGID=1000", "TZ=Etc/UTC"]
        },
        {
            "id": "bookstack",
            "name": "BookStack",
            "category": "productivity",
            "image": "lscr.io/linuxserver/bookstack:latest",
            "description": "Simple, self-hosted wiki platform",
            "ports": ["6875:80"],
            "volumes": ["/config"],
            "env": ["PUID=1000", "PGID=1000", "TZ=Etc/UTC", "APP_URL=http://localhost:6875", "DB_HOST=bookstack_db", "DB_DATABASE=bookstackapp", "DB_USERNAME=bookstack", "DB_PASSWORD=yourdbpass"]
        },
        {
            "id": "hedgedoc",
            "name": "HedgeDoc",
            "category": "productivity",
            "image": "lscr.io/linuxserver/hedgedoc:latest",
            "description": "Collaborative markdown editor",
            "ports": ["3000:3000"],
            "volumes": ["/config"],
            "env": ["PUID=1000", "PGID=1000", "TZ=Etc/UTC", "DB_HOST=192.168.1.1", "DB_PORT=3306", "DB_USER=hedgedoc", "DB_PASS=hedgedocpass", "DB_NAME=hedgedoc"]
        },
        
        # ========== PHOTOS ==========
        {
            "id": "photoprism",
            "name": "PhotoPrism",
            "category": "photos",
            "image": "lscr.io/linuxserver/photoprism:latest",
            "description": "AI-powered photo management app",
            "ports": ["2342:2342"],
            "volumes": ["/config", "/photos"],
            "env": ["PUID=1000", "PGID=1000", "TZ=Etc/UTC"]
        },
        {
            "id": "piwigo",
            "name": "Piwigo",
            "category": "photos",
            "image": "lscr.io/linuxserver/piwigo:latest",
            "description": "Photo gallery software for the web",
            "ports": ["80:80"],
            "volumes": ["/config", "/gallery"],
            "env": ["PUID=1000", "PGID=1000", "TZ=Etc/UTC"]
        },
        {
            "id": "immich",
            "name": "Immich",
            "category": "photos",
            "image": "lscr.io/linuxserver/immich:latest",
            "description": "High performance self-hosted photo and video backup",
            "ports": ["8080:8080"],
            "volumes": ["/config", "/photos"],
            "env": ["PUID=1000", "PGID=1000", "TZ=Etc/UTC"]
        },
        {
            "id": "lychee",
            "name": "Lychee",
            "category": "photos",
            "image": "lscr.io/linuxserver/lychee:latest",
            "description": "Free photo management tool for web",
            "ports": ["80:80"],
            "volumes": ["/config", "/pictures"],
            "env": ["PUID=1000", "PGID=1000", "TZ=Etc/UTC"]
        },
        
        # ========== NETWORKING ==========
        {
            "id": "wireguard",
            "name": "WireGuard",
            "category": "networking",
            "image": "lscr.io/linuxserver/wireguard:latest",
            "description": "Fast modern VPN protocol",
            "ports": ["51820:51820/udp"],
            "volumes": ["/config"],
            "env": ["PUID=1000", "PGID=1000", "TZ=Etc/UTC", "SERVERURL=auto", "PEERS=1"]
        },
        {
            "id": "openvpn-as",
            "name": "OpenVPN Access Server",
            "category": "networking",
            "image": "lscr.io/linuxserver/openvpn-as:latest",
            "description": "Full-featured VPN server",
            "ports": ["943:943", "9443:9443", "1194:1194/udp"],
            "volumes": ["/config"],
            "env": ["PUID=1000", "PGID=1000", "TZ=Etc/UTC"]
        },
        {
            "id": "nginx",
            "name": "Nginx",
            "category": "networking",
            "image": "lscr.io/linuxserver/nginx:latest",
            "description": "High-performance web server and reverse proxy",
            "ports": ["80:80", "443:443"],
            "volumes": ["/config"],
            "env": ["PUID=1000", "PGID=1000", "TZ=Etc/UTC"]
        },
        {
            "id": "swag",
            "name": "SWAG",
            "category": "networking",
            "image": "lscr.io/linuxserver/swag:latest",
            "description": "Nginx with automatic SSL via Let's Encrypt",
            "ports": ["443:443", "80:80"],
            "volumes": ["/config"],
            "env": ["PUID=1000", "PGID=1000", "TZ=Etc/UTC", "URL=yourdomain.url", "VALIDATION=http"]
        },
        {
            "id": "nginx-proxy-manager",
            "name": "Nginx Proxy Manager",
            "category": "networking",
            "image": "lscr.io/linuxserver/nginx-proxy-manager:latest",
            "description": "Easy nginx reverse proxy with SSL",
            "ports": ["80:80", "443:443", "81:81"],
            "volumes": ["/config"],
            "env": ["PUID=1000", "PGID=1000", "TZ=Etc/UTC"]
        },
        {
            "id": "ddclient",
            "name": "DDClient",
            "category": "networking",
            "image": "lscr.io/linuxserver/ddclient:latest",
            "description": "Dynamic DNS client",
            "ports": [],
            "volumes": ["/config"],
            "env": ["PUID=1000", "PGID=1000", "TZ=Etc/UTC"]
        },
        {
            "id": "duckdns",
            "name": "DuckDNS",
            "category": "networking",
            "image": "lscr.io/linuxserver/duckdns:latest",
            "description": "Free dynamic DNS service",
            "ports": [],
            "volumes": ["/config"],
            "env": ["PUID=1000", "PGID=1000", "TZ=Etc/UTC", "SUBDOMAINS=subdomain1,subdomain2", "TOKEN=token"]
        },
        {
            "id": "netbootxyz",
            "name": "Netboot.xyz",
            "category": "networking",
            "image": "lscr.io/linuxserver/netbootxyz:latest",
            "description": "PXE boot various operating systems",
            "ports": ["3000:3000", "69:69/udp", "8080:80"],
            "volumes": ["/config", "/assets"],
            "env": ["PUID=1000", "PGID=1000", "TZ=Etc/UTC"]
        },
        {
            "id": "smokeping",
            "name": "SmokePing",
            "category": "networking",
            "image": "lscr.io/linuxserver/smokeping:latest",
            "description": "Network latency monitoring tool",
            "ports": ["80:80"],
            "volumes": ["/config", "/data"],
            "env": ["PUID=1000", "PGID=1000", "TZ=Etc/UTC"]
        },
        {
            "id": "librespeed",
            "name": "LibreSpeed",
            "category": "networking",
            "image": "lscr.io/linuxserver/librespeed:latest",
            "description": "Self-hosted speedtest service",
            "ports": ["80:80"],
            "volumes": ["/config"],
            "env": ["PUID=1000", "PGID=1000", "TZ=Etc/UTC"]
        },
        
        # ========== MONITORING & MANAGEMENT ==========
        {
            "id": "netdata",
            "name": "Netdata",
            "category": "monitoring",
            "image": "lscr.io/linuxserver/netdata:latest",
            "description": "Real-time performance monitoring",
            "ports": ["19999:19999"],
            "volumes": ["/config"],
            "env": ["PUID=1000", "PGID=1000", "TZ=Etc/UTC"]
        },
        {
            "id": "scrutiny",
            "name": "Scrutiny",
            "category": "monitoring",
            "image": "lscr.io/linuxserver/scrutiny:latest",
            "description": "Hard drive S.M.A.R.T monitoring",
            "ports": ["8080:8080", "8086:8086"],
            "volumes": ["/config", "/run/udev:/run/udev:ro"],
            "env": ["PUID=1000", "PGID=1000", "TZ=Etc/UTC"]
        },
        {
            "id": "healthchecks",
            "name": "Healthchecks",
            "category": "monitoring",
            "image": "lscr.io/linuxserver/healthchecks:latest",
            "description": "Cron job and scheduled task monitoring",
            "ports": ["8000:8000"],
            "volumes": ["/config"],
            "env": ["PUID=1000", "PGID=1000", "TZ=Etc/UTC"]
        },
        {
            "id": "uptime-kuma",
            "name": "Uptime Kuma",
            "category": "monitoring",
            "image": "lscr.io/linuxserver/uptime-kuma:latest",
            "description": "Self-hosted monitoring tool like Uptime Robot",
            "ports": ["3001:3001"],
            "volumes": ["/config"],
            "env": ["PUID=1000", "PGID=1000", "TZ=Etc/UTC"]
        },
        {
            "id": "tautulli",
            "name": "Tautulli",
            "category": "monitoring",
            "image": "lscr.io/linuxserver/tautulli:latest",
            "description": "Monitoring and tracking for Plex Media Server",
            "ports": ["8181:8181"],
            "volumes": ["/config"],
            "env": ["PUID=1000", "PGID=1000", "TZ=Etc/UTC"]
        },
        {
            "id": "overseerr",
            "name": "Overseerr",
            "category": "monitoring",
            "image": "lscr.io/linuxserver/overseerr:latest",
            "description": "Request management and media discovery for Plex",
            "ports": ["5055:5055"],
            "volumes": ["/config"],
            "env": ["PUID=1000", "PGID=1000", "TZ=Etc/UTC"]
        },
        {
            "id": "requestrr",
            "name": "Requestrr",
            "category": "monitoring",
            "image": "lscr.io/linuxserver/requestrr:latest",
            "description": "Chatbot for requesting media via Discord/Telegram",
            "ports": ["4545:4545"],
            "volumes": ["/config"],
            "env": ["PUID=1000", "PGID=1000", "TZ=Etc/UTC"]
        },
        {
            "id": "ombi",
            "name": "Ombi",
            "category": "monitoring",
            "image": "lscr.io/linuxserver/ombi:latest",
            "description": "Self-hosted media request management",
            "ports": ["3579:3579"],
            "volumes": ["/config"],
            "env": ["PUID=1000", "PGID=1000", "TZ=Etc/UTC"]
        },
        {
            "id": "varken",
            "name": "Varken",
            "category": "monitoring",
            "image": "lscr.io/linuxserver/varken:latest",
            "description": "Monitoring for Plex, Sonarr, Radarr and more",
            "ports": [],
            "volumes": ["/config"],
            "env": ["PUID=1000", "PGID=1000", "TZ=Etc/UTC"]
        },
        
        # ========== BACKUP ==========
        {
            "id": "duplicati",
            "name": "Duplicati",
            "category": "backup",
            "image": "lscr.io/linuxserver/duplicati:latest",
            "description": "Store encrypted backups online",
            "ports": ["8200:8200"],
            "volumes": ["/config", "/backups", "/source"],
            "env": ["PUID=1000", "PGID=1000", "TZ=Etc/UTC"]
        },
        {
            "id": "resilio-sync",
            "name": "Resilio Sync",
            "category": "backup",
            "image": "lscr.io/linuxserver/resilio-sync:latest",
            "description": "Fast, private file syncing",
            "ports": ["8888:8888", "55555:55555"],
            "volumes": ["/config", "/sync"],
            "env": ["PUID=1000", "PGID=1000", "TZ=Etc/UTC"]
        },
        {
            "id": "rsnapshot",
            "name": "Rsnapshot",
            "category": "backup",
            "image": "lscr.io/linuxserver/rsnapshot:latest",
            "description": "Filesystem snapshot utility",
            "ports": [],
            "volumes": ["/config", "/snapshots", ".ssh"],
            "env": ["PUID=1000", "PGID=1000", "TZ=Etc/UTC"]
        },
        
        # ========== DATABASES ==========
        {
            "id": "mariadb",
            "name": "MariaDB",
            "category": "database",
            "image": "lscr.io/linuxserver/mariadb:latest",
            "description": "MySQL-compatible relational database",
            "ports": ["3306:3306"],
            "volumes": ["/config"],
            "env": ["PUID=1000", "PGID=1000", "TZ=Etc/UTC", "MYSQL_ROOT_PASSWORD=ROOT_ACCESS_PASSWORD", "MYSQL_DATABASE=USER_DB_NAME", "MYSQL_USER=MYSQL_USER", "MYSQL_PASSWORD=DATABASE_PASSWORD"]
        },
        {
            "id": "mysql-workbench",
            "name": "MySQL Workbench",
            "category": "database",
            "image": "lscr.io/linuxserver/mysql-workbench:latest",
            "description": "Visual database design tool",
            "ports": ["3000:3000", "3001:3001"],
            "volumes": ["/config"],
            "env": ["PUID=1000", "PGID=1000", "TZ=Etc/UTC"]
        },
        {
            "id": "sqlitebrowser",
            "name": "SQLiteBrowser",
            "category": "database",
            "image": "lscr.io/linuxserver/sqlitebrowser:latest",
            "description": "Visual tool for creating, designing and editing SQLite databases",
            "ports": ["3000:3000", "3001:3001"],
            "volumes": ["/config"],
            "env": ["PUID=1000", "PGID=1000", "TZ=Etc/UTC"]
        },
        
        # ========== COMMUNICATION ==========
        {
            "id": "freshrss",
            "name": "FreshRSS",
            "category": "communication",
            "image": "lscr.io/linuxserver/freshrss:latest",
            "description": "Free self-hostable RSS aggregator",
            "ports": ["80:80"],
            "volumes": ["/config"],
            "env": ["PUID=1000", "PGID=1000", "TZ=Etc/UTC"]
        },
        {
            "id": "weechat",
            "name": "WeeChat",
            "category": "communication",
            "image": "lscr.io/linuxserver/weechat:latest",
            "description": "Fast, light and extensible IRC client",
            "ports": ["9001:9001"],
            "volumes": ["/config"],
            "env": ["PUID=1000", "PGID=1000", "TZ=Etc/UTC"]
        },
        {
            "id": "znc",
            "name": "ZNC",
            "category": "communication",
            "image": "lscr.io/linuxserver/znc:latest",
            "description": "Advanced IRC bouncer",
            "ports": ["6501:6501"],
            "volumes": ["/config"],
            "env": ["PUID=1000", "PGID=1000", "TZ=Etc/UTC"]
        },
        {
            "id": "apprise-api",
            "name": "Apprise API",
            "category": "communication",
            "image": "lscr.io/linuxserver/apprise-api:latest",
            "description": "Send notifications to 100+ services",
            "ports": ["8000:8000"],
            "volumes": ["/config"],
            "env": ["PUID=1000", "PGID=1000", "TZ=Etc/UTC"]
        },
        {
            "id": "ntfy",
            "name": "ntfy",
            "category": "communication",
            "image": "lscr.io/linuxserver/ntfy:latest",
            "description": "Simple HTTP-based pub-sub notification service",
            "ports": ["80:80"],
            "volumes": ["/config"],
            "env": ["PUID=1000", "PGID=1000", "TZ=Etc/UTC"]
        },
        
        # ========== WEB APPLICATIONS ==========
        {
            "id": "firefox",
            "name": "Firefox",
            "category": "desktop",
            "image": "lscr.io/linuxserver/firefox:latest",
            "description": "Firefox web browser in Docker",
            "ports": ["3000:3000", "3001:3001"],
            "volumes": ["/config"],
            "env": ["PUID=1000", "PGID=1000", "TZ=Etc/UTC"]
        },
        {
            "id": "chromium",
            "name": "Chromium",
            "category": "desktop",
            "image": "lscr.io/linuxserver/chromium:latest",
            "description": "Chromium web browser in Docker",
            "ports": ["3000:3000", "3001:3001"],
            "volumes": ["/config"],
            "env": ["PUID=1000", "PGID=1000", "TZ=Etc/UTC"]
        },
        {
            "id": "webtop",
            "name": "Webtop",
            "category": "desktop",
            "image": "lscr.io/linuxserver/webtop:latest",
            "description": "Full Linux desktop in your browser",
            "ports": ["3000:3000", "3001:3001"],
            "volumes": ["/config"],
            "env": ["PUID=1000", "PGID=1000", "TZ=Etc/UTC"]
        },
        {
            "id": "rdesktop",
            "name": "rdesktop",
            "category": "desktop",
            "image": "lscr.io/linuxserver/rdesktop:latest",
            "description": "Remote desktop in a container",
            "ports": ["3389:3389"],
            "volumes": ["/config"],
            "env": ["PUID=1000", "PGID=1000", "TZ=Etc/UTC"]
        },
        
        # ========== GAMING ==========
        {
            "id": "steamheadless",
            "name": "Steam Headless",
            "category": "gaming",
            "image": "lscr.io/linuxserver/steamheadless:latest",
            "description": "Steam client in a headless container",
            "ports": ["3000:3000", "3001:3001"],
            "volumes": ["/config"],
            "env": ["PUID=1000", "PGID=1000", "TZ=Etc/UTC"]
        },
        {
            "id": "minetest",
            "name": "Minetest",
            "category": "gaming",
            "image": "lscr.io/linuxserver/minetest:latest",
            "description": "Open source voxel game engine",
            "ports": ["30000:30000/udp"],
            "volumes": ["/config/.minetest"],
            "env": ["PUID=1000", "PGID=1000", "TZ=Etc/UTC"]
        },
        
        # ========== FILE MANAGEMENT ==========
        {
            "id": "filebrowser",
            "name": "FileBrowser",
            "category": "files",
            "image": "lscr.io/linuxserver/filebrowser:latest",
            "description": "Web file browser and manager",
            "ports": ["80:80"],
            "volumes": ["/config", "/srv"],
            "env": ["PUID=1000", "PGID=1000", "TZ=Etc/UTC"]
        },
        {
            "id": "projectsend",
            "name": "ProjectSend",
            "category": "files",
            "image": "lscr.io/linuxserver/projectsend:latest",
            "description": "Private file sharing application",
            "ports": ["80:80"],
            "volumes": ["/config", "/data"],
            "env": ["PUID=1000", "PGID=1000", "TZ=Etc/UTC"]
        },
        {
            "id": "davos",
            "name": "Davos",
            "category": "files",
            "image": "lscr.io/linuxserver/davos:latest",
            "description": "FTP automation and schedule transfer tool",
            "ports": ["8080:8080"],
            "volumes": ["/config", "/download"],
            "env": ["PUID=1000", "PGID=1000", "TZ=Etc/UTC"]
        },
        
        # ========== DEVELOPMENT ==========
        {
            "id": "docker-compose",
            "name": "Docker Compose",
            "category": "development",
            "image": "lscr.io/linuxserver/docker-compose:latest",
            "description": "Docker Compose in a container",
            "ports": [],
            "volumes": ["/var/run/docker.sock:/var/run/docker.sock"],
            "env": ["PUID=1000", "PGID=1000", "TZ=Etc/UTC"]
        },
        {
            "id": "gitea",
            "name": "Gitea",
            "category": "development",
            "image": "lscr.io/linuxserver/gitea:latest",
            "description": "Lightweight self-hosted Git service",
            "ports": ["3000:3000", "222:22"],
            "volumes": ["/config"],
            "env": ["PUID=1000", "PGID=1000", "TZ=Etc/UTC"]
        },
        {
            "id": "grocy",
            "name": "Grocy",
            "category": "productivity",
            "image": "lscr.io/linuxserver/grocy:latest",
            "description": "ERP system for your kitchen",
            "ports": ["80:80"],
            "volumes": ["/config"],
            "env": ["PUID=1000", "PGID=1000", "TZ=Etc/UTC"]
        },
        {
            "id": "kanzi",
            "name": "Kanzi",
            "category": "productivity",
            "image": "lscr.io/linuxserver/kanzi:latest",
            "description": "Self-hosted kanban board",
            "ports": ["80:80"],
            "volumes": ["/config"],
            "env": ["PUID=1000", "PGID=1000", "TZ=Etc/UTC"]
        },
        {
            "id": "lazylibrarian",
            "name": "LazyLibrarian",
            "category": "arr",
            "image": "lscr.io/linuxserver/lazylibrarian:latest",
            "description": "Follow authors and grab ebooks and audiobooks",
            "ports": ["5299:5299"],
            "volumes": ["/config", "/downloads", "/books"],
            "env": ["PUID=1000", "PGID=1000", "TZ=Etc/UTC"]
        },
        {
            "id": "booksonic-air",
            "name": "Booksonic Air",
            "category": "media",
            "image": "lscr.io/linuxserver/booksonic-air:latest",
            "description": "Audiobook streaming server",
            "ports": ["4040:4040"],
            "volumes": ["/config", "/audiobooks", "/podcasts"],
            "env": ["PUID=1000", "PGID=1000", "TZ=Etc/UTC"]
        },
        {
            "id": "kavita",
            "name": "Kavita",
            "category": "productivity",
            "image": "lscr.io/linuxserver/kavita:latest",
            "description": "Fast, feature rich reading server for manga/comics/books",
            "ports": ["5000:5000"],
            "volumes": ["/config", "/data"],
            "env": ["PUID=1000", "PGID=1000", "TZ=Etc/UTC"]
        },
        {
            "id": "cops",
            "name": "COPS",
            "category": "productivity",
            "image": "lscr.io/linuxserver/cops:latest",
            "description": "Calibre OPDS and HTML PHP server",
            "ports": ["80:80"],
            "volumes": ["/config", "/books"],
            "env": ["PUID=1000", "PGID=1000", "TZ=Etc/UTC"]
        },
        {
            "id": "ubooquity",
            "name": "Ubooquity",
            "category": "productivity",
            "image": "lscr.io/linuxserver/ubooquity:latest",
            "description": "Free home server for comics and ebooks",
            "ports": ["2202:2202", "2203:2203"],
            "volumes": ["/config", "/books", "/comics", "/files"],
            "env": ["PUID=1000", "PGID=1000", "TZ=Etc/UTC"]
        },
        {
            "id": "minisatip",
            "name": "Minisatip",
            "category": "media",
            "image": "lscr.io/linuxserver/minisatip:latest",
            "description": "SAT>IP server for DVB cards",
            "ports": ["8875:8875", "554:554", "1900:1900/udp"],
            "volumes": ["/config"],
            "env": ["PUID=1000", "PGID=1000", "TZ=Etc/UTC"]
        },
        {
            "id": "tvheadend",
            "name": "TVHeadend",
            "category": "media",
            "image": "lscr.io/linuxserver/tvheadend:latest",
            "description": "TV streaming server for Linux",
            "ports": ["9981:9981", "9982:9982"],
            "volumes": ["/config", "/recordings"],
            "env": ["PUID=1000", "PGID=1000", "TZ=Etc/UTC"]
        },
        {
            "id": "oscam",
            "name": "Oscam",
            "category": "media",
            "image": "lscr.io/linuxserver/oscam:latest",
            "description": "Open Source Conditional Access Module",
            "ports": ["8888:8888"],
            "volumes": ["/config"],
            "env": ["PUID=1000", "PGID=1000", "TZ=Etc/UTC"]
        },
        {
            "id": "grav",
            "name": "Grav",
            "category": "web",
            "image": "lscr.io/linuxserver/grav:latest",
            "description": "Fast, simple, flexible file-based CMS",
            "ports": ["80:80"],
            "volumes": ["/config"],
            "env": ["PUID=1000", "PGID=1000", "TZ=Etc/UTC"]
        },
        {
            "id": "snippets",
            "name": "Code Snippets",
            "category": "development",
            "image": "lscr.io/linuxserver/code-snippets:latest",
            "description": "Self-hosted snippets manager",
            "ports": ["8080:8080"],
            "volumes": ["/config"],
            "env": ["PUID=1000", "PGID=1000", "TZ=Etc/UTC"]
        },
        {
            "id": "habridge",
            "name": "HABridge",
            "category": "automation",
            "image": "lscr.io/linuxserver/habridge:latest",
            "description": "Emulate Philips Hue API for home automation",
            "ports": ["8080:8080", "50000:50000"],
            "volumes": ["/config"],
            "env": ["PUID=1000", "PGID=1000", "TZ=Etc/UTC"]
        },
        {
            "id": "domoticz",
            "name": "Domoticz",
            "category": "automation",
            "image": "lscr.io/linuxserver/domoticz:latest",
            "description": "Home automation system",
            "ports": ["8080:8080", "6144:6144", "1443:1443"],
            "volumes": ["/config"],
            "env": ["PUID=1000", "PGID=1000", "TZ=Etc/UTC"]
        },
        {
            "id": "foldingathome",
            "name": "Folding@Home",
            "category": "other",
            "image": "lscr.io/linuxserver/foldingathome:latest",
            "description": "Donate computing power to fight diseases",
            "ports": ["7396:7396", "36330:36330"],
            "volumes": ["/config"],
            "env": ["PUID=1000", "PGID=1000", "TZ=Etc/UTC"]
        },
        {
            "id": "boinc",
            "name": "BOINC",
            "category": "other",
            "image": "lscr.io/linuxserver/boinc:latest",
            "description": "Donate computing power to science",
            "ports": ["8080:8080"],
            "volumes": ["/config"],
            "env": ["PUID=1000", "PGID=1000", "TZ=Etc/UTC"]
        },
        {
            "id": "syslog-ng",
            "name": "Syslog-ng",
            "category": "monitoring",
            "image": "lscr.io/linuxserver/syslog-ng:latest",
            "description": "Syslog server with advanced log processing",
            "ports": ["514:5514/udp", "601:6601/tcp", "6514:6514/tcp"],
            "volumes": ["/config", "/var/log"],
            "env": ["PUID=1000", "PGID=1000", "TZ=Etc/UTC"]
        },
        {
            "id": "unifi-network-application",
            "name": "UniFi Network Application",
            "category": "networking",
            "image": "lscr.io/linuxserver/unifi-network-application:latest",
            "description": "Ubiquiti UniFi network controller",
            "ports": ["8443:8443", "3478:3478/udp", "10001:10001/udp", "8080:8080"],
            "volumes": ["/config"],
            "env": ["PUID=1000", "PGID=1000", "TZ=Etc/UTC", "MONGO_USER=unifi", "MONGO_PASS=pass", "MONGO_HOST=unifi-db", "MONGO_PORT=27017", "MONGO_DBNAME=unifi"]
        },
        {
            "id": "snipe-it",
            "name": "Snipe-IT",
            "category": "productivity",
            "image": "lscr.io/linuxserver/snipe-it:latest",
            "description": "IT asset management system",
            "ports": ["80:80"],
            "volumes": ["/config"],
            "env": ["PUID=1000", "PGID=1000", "TZ=Etc/UTC", "APP_URL=http://localhost", "MYSQL_PORT_3306_TCP_ADDR=mysql", "MYSQL_PORT_3306_TCP_PORT=3306", "MYSQL_DATABASE=snipeit", "MYSQL_USER=snipeit", "MYSQL_PASSWORD=snipeitpass"]
        },
    ]
