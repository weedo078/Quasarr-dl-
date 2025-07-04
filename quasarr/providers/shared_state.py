# -*- coding: utf-8 -*-
# Quasarr
# Project by https://github.com/rix1337

import json
import os
import re
import time
import traceback
from datetime import datetime, timedelta
from urllib import parse

import quasarr
from quasarr.providers.log import info, debug
from quasarr.providers.myjd_api import Myjdapi, TokenExpiredException, RequestTimeoutException, MYJDException, Jddevice
from quasarr.storage.config import Config
from quasarr.storage.sqlite_database import DataBase

values = {}
lock = None

# regex to detect season/episode tags for series filtering during search
SEASON_EP_REGEX = re.compile(r"(?i)(?:S\d{1,3}(?:E\d{1,3}(?:-\d{1,3})?)?|S\d{1,3}-\d{1,3})")
# regex to filter out season/episode tags for movies
MOVIE_REGEX = re.compile(r"^(?!.*(?:S\d{1,3}(?:E\d{1,3}(?:-\d{1,3})?)?|S\d{1,3}-\d{1,3})).*$", re.IGNORECASE)


def set_state(manager_dict, manager_lock):
    global values
    global lock
    values = manager_dict
    lock = manager_lock


def update(key, value):
    global values
    global lock
    lock.acquire()
    try:
        values[key] = value
    finally:
        lock.release()


def set_connection_info(internal_address, external_address, port):
    if internal_address.count(":") < 2:
        internal_address = f"{internal_address}:{port}"
    update("internal_address", internal_address)
    update("external_address", external_address)
    update("port", port)


def set_files(config_path):
    update("configfile", os.path.join(config_path, "Quasarr.ini"))
    update("dbfile", os.path.join(config_path, "Quasarr.db"))


def generate_api_key():
    api_key = os.urandom(32).hex()
    Config('API').save("key", api_key)
    info(f'API key replaced with: "{api_key}!"')
    return api_key


def extract_valid_hostname(url, shorthand):
    # Check if both characters from the shorthand appear in the url
    try:
        if '://' not in url:
            url = 'http://' + url
        result = parse.urlparse(url)
        domain = result.netloc

        if not "." in domain:
            print(f'Invalid domain "{domain}": No "." found')
            return None

        if all(char in domain for char in shorthand):
            print(f'"{domain}" matches both characters from "{shorthand}". Continuing...')
            return domain
        else:
            print(f'Invalid domain "{domain}": Does not contain both characters from shorthand "{shorthand}"')
            return None
    except Exception as e:
        print(f"Error parsing URL {url}: {e}")
        return None


def connect_to_jd(jd, user, password, device_name):
    try:
        jd.connect(user, password)
        jd.update_devices()
        device = jd.get_device(device_name)
    except (TokenExpiredException, RequestTimeoutException, MYJDException) as e:
        info("Error connecting to JDownloader: " + str(e).strip())
        return False
    if not device or not isinstance(device, (type, Jddevice)):
        return False
    else:
        device.downloadcontroller.get_current_state()  # request forces direct_connection info update
        connection_info = device.check_direct_connection()
        if connection_info["status"]:
            info(f'Direct connection to JDownloader established: "{connection_info['ip']}"')
        else:
            info("Could not establish direct connection to JDownloader.")
        update("device", device)
        return True


def set_device(user, password, device):
    jd = Myjdapi()
    jd.set_app_key('Quasarr')
    return connect_to_jd(jd, user, password, device)


def set_device_from_config():
    config = Config('JDownloader')
    user = str(config.get('user'))
    password = str(config.get('password'))
    device = str(config.get('device'))

    update("device", device)

    if user and password and device:
        jd = Myjdapi()
        jd.set_app_key('Quasarr')
        return connect_to_jd(jd, user, password, device)
    return False


def check_device(device):
    try:
        valid = isinstance(device,
                           (type, Jddevice)) and device.downloadcontroller.get_current_state()
    except (AttributeError, KeyError, TokenExpiredException, RequestTimeoutException, MYJDException):
        valid = False
    return valid


def connect_device():
    config = Config('JDownloader')
    user = str(config.get('user'))
    password = str(config.get('password'))
    device = str(config.get('device'))

    jd = Myjdapi()
    jd.set_app_key('Quasarr')

    if user and password and device:
        try:
            jd.connect(user, password)
            jd.update_devices()
            device = jd.get_device(device)
        except (TokenExpiredException, RequestTimeoutException, MYJDException):
            pass

    if check_device(device):
        update("device", device)
        return True
    else:
        return False


def get_device():
    attempts = 0

    while True:
        try:
            if check_device(values["device"]):
                break
        except (AttributeError, KeyError, TokenExpiredException, RequestTimeoutException, MYJDException):
            pass
        attempts += 1

        update("device", False)

        if attempts % 10 == 0:
            info(
                f"WARNING: {attempts} consecutive JDownloader connection errors. Please check your credentials!")
        time.sleep(3)

        if connect_device():
            break

    return values["device"]


def get_devices(user, password):
    jd = Myjdapi()
    jd.set_app_key('Quasarr')
    try:
        jd.connect(user, password)
        jd.update_devices()
        devices = jd.list_devices()
        return devices
    except (TokenExpiredException, RequestTimeoutException, MYJDException) as e:
        info("Error connecting to JDownloader: " + str(e))
        return []


def set_device_settings():
    device = get_device()

    settings_to_enforce = [
        {
            "namespace": "org.jdownloader.settings.GeneralSettings",
            "storage": None,
            "setting": "AutoStartDownloadOption",
            "expected_value": "ALWAYS",  # Downloads must start automatically for Quasarr to work
        },
        {
            "namespace": "org.jdownloader.settings.GeneralSettings",
            "storage": None,
            "setting": "IfFileExistsAction",
            "expected_value": "SKIP_FILE",  # Prevents popups during download
        },
        {
            "namespace": "org.jdownloader.settings.GeneralSettings",
            "storage": None,
            "setting": "CleanupAfterDownloadAction",
            "expected_value": "NEVER",  # Links must be kept after download for Quasarr to work
        },
        {
            "namespace": "org.jdownloader.settings.GraphicalUserInterfaceSettings",
            "storage": None,
            "setting": "BannerEnabled",
            "expected_value": False,  # Removes UI clutter in JDownloader
        },
        {
            "namespace": "org.jdownloader.settings.GraphicalUserInterfaceSettings",
            "storage": None,
            "setting": "DonateButtonState",
            "expected_value": "CUSTOM_HIDDEN",  # Removes UI clutter in JDownloader
        },
        {
            "namespace": "org.jdownloader.extensions.extraction.ExtractionConfig",
            "storage": "cfg/org.jdownloader.extensions.extraction.ExtractionExtension",
            "setting": "DeleteArchiveFilesAfterExtractionAction",
            "expected_value": "NULL",  # "NULL" is the ENUM for "Delete files from Harddisk"
        },
        {
            "namespace": "org.jdownloader.extensions.extraction.ExtractionConfig",
            "storage": "cfg/org.jdownloader.extensions.extraction.ExtractionExtension",
            "setting": "IfFileExistsAction",
            "expected_value": "OVERWRITE_FILE",  # Prevents popups during extraction
        },
        {
            "namespace": "org.jdownloader.extensions.extraction.ExtractionConfig",
            "storage": "cfg/org.jdownloader.extensions.extraction.ExtractionExtension",
            "setting": "DeleteArchiveDownloadlinksAfterExtraction",
            "expected_value": False,  # Links must be kept after extraction for Quasarr to work
        },
        {
            "namespace": "org.jdownloader.gui.views.linkgrabber.addlinksdialog.LinkgrabberSettings",
            "storage": None,
            "setting": "OfflinePackageEnabled",
            "expected_value": False,  # Don't move offline links to extra package
        },
        {
            "namespace": "org.jdownloader.gui.views.linkgrabber.addlinksdialog.LinkgrabberSettings",
            "storage": None,
            "setting": "HandleOfflineOnConfirmLatestSelection",
            "expected_value": "INCLUDE_OFFLINE",  # Offline links must always be kept for Quasarr to handle packages
        },
        {
            "namespace": "org.jdownloader.gui.views.linkgrabber.addlinksdialog.LinkgrabberSettings",
            "storage": None,
            "setting": "AutoConfirmManagerHandleOffline",
            "expected_value": "INCLUDE_OFFLINE",  # Offline links must always be kept for Quasarr to handle packages
        },
        {
            "namespace": "org.jdownloader.gui.views.linkgrabber.addlinksdialog.LinkgrabberSettings",
            "storage": None,
            "setting": "DefaultOnAddedOfflineLinksAction",
            "expected_value": "INCLUDE_OFFLINE",  # Offline links must always be kept for Quasarr to handle packages
        },
    ]

    for setting in settings_to_enforce:
        namespace = setting["namespace"]
        storage = setting["storage"] or "null"
        name = setting["setting"]
        expected_value = setting["expected_value"]

        settings = device.config.get(namespace, storage, name)

        if settings != expected_value:
            success = device.config.set(namespace, storage, name, expected_value)

            location = f"{namespace}/{storage}" if storage != "null" else namespace
            status = "Updated" if success else "Failed to update"
            info(f'{status} "{name}" in "{location}" to "{expected_value}".')

    settings_to_add = [
        {
            "namespace": "org.jdownloader.extensions.extraction.ExtractionConfig",
            "storage": "cfg/org.jdownloader.extensions.extraction.ExtractionExtension",
            "setting": "BlacklistPatterns",
            "expected_values": [
                '.*sample/.*',
                '.*Sample/.*',
                '.*\\.jpe?g',
                '.*\\.idx',
                '.*\\.sub',
                '.*\\.srt',
                '.*\\.nfo',
                '.*\\.bat',
                '.*\\.txt',
                '.*\\.exe',
                '.*\\.sfv'
            ]
        },
        {
            "namespace": "org.jdownloader.controlling.filter.LinkFilterSettings",
            "storage": "null",
            "setting": "FilterList",
            "expected_values": [
                {'conditionFilter':
                     {'conditions': [], 'enabled': False, 'matchType': 'IS_TRUE'}, 'created': 0,
                 'enabled': True,
                 'filenameFilter': {
                     'enabled': True,
                     'matchType': 'CONTAINS',
                     'regex': '.*\\.(sfv|jpe?g|idx|srt|nfo|bat|txt|exe)',
                     'useRegex': True
                 },
                 'filesizeFilter': {'enabled': False, 'from': 0, 'matchType': 'BETWEEN', 'to': 0},
                 'filetypeFilter': {'archivesEnabled': False, 'audioFilesEnabled': False, 'customs': None,
                                    'docFilesEnabled': False, 'enabled': False, 'exeFilesEnabled': False,
                                    'hashEnabled': False, 'imagesEnabled': False, 'matchType': 'IS',
                                    'subFilesEnabled': False, 'useRegex': False, 'videoFilesEnabled': False},
                 'hosterURLFilter': {'enabled': False, 'matchType': 'CONTAINS', 'regex': '', 'useRegex': False},
                 'matchAlwaysFilter': {'enabled': False}, 'name': 'Quasarr_Block_Files',
                 'onlineStatusFilter': {'enabled': False, 'matchType': 'IS', 'onlineStatus': 'OFFLINE'},
                 'originFilter': {'enabled': False, 'matchType': 'IS', 'origins': []},
                 'packagenameFilter': {'enabled': False, 'matchType': 'CONTAINS', 'regex': '', 'useRegex': False},
                 'pluginStatusFilter': {'enabled': False, 'matchType': 'IS', 'pluginStatus': 'PREMIUM'},
                 'sourceURLFilter': {'enabled': False, 'matchType': 'CONTAINS', 'regex': '', 'useRegex': False},
                 'testUrl': ''}]
        },
    ]

    for setting in settings_to_add:
        namespace = setting["namespace"]
        storage = setting["storage"] or "null"
        name = setting["setting"]
        expected_values = setting["expected_values"]

        added_items = 0
        settings = device.config.get(namespace, storage, name)
        for item in expected_values:
            if item not in settings:
                settings.append(item)
                added_items += 1

        if added_items:
            success = device.config.set(namespace, storage, name, json.dumps(settings))

            location = f"{namespace}/{storage}" if storage != "null" else namespace
            status = "Added" if success else "Failed to add"
            info(f'{status} {added_items} items to "{name}" in "{location}".')


def update_jdownloader():
    try:
        if not get_device():
            set_device_from_config()
        device = get_device()

        if device:
            try:
                current_state = device.downloadcontroller.get_current_state()
                is_collecting = device.linkgrabber.is_collecting()
                update_available = device.update.update_available()

                if (current_state.lower() == "idle") and (not is_collecting and update_available):
                    info("JDownloader update ready. Starting update...")
                    device.update.restart_and_update()
            except quasarr.providers.myjd_api.TokenExpiredException:
                return False
            return True
        else:
            return False
    except quasarr.providers.myjd_api.MYJDException as e:
        info(f"Error updating JDownloader: {e}")
        return False


def start_downloads():
    try:
        if not get_device():
            set_device_from_config()
        device = get_device()

        if device:
            try:
                return device.downloadcontroller.start_downloads()
            except quasarr.providers.myjd_api.TokenExpiredException:
                return False
        else:
            return False
    except quasarr.providers.myjd_api.MYJDException as e:
        info(f"Error starting Downloads: {e}")
        return False


def get_db(table):
    return DataBase(table)


def convert_to_mb(item):
    size = float(item['size'])
    unit = item['sizeunit'].upper()

    if unit == 'B':
        size_b = size
    elif unit == 'KB':
        size_b = size * 1024
    elif unit == 'MB':
        size_b = size * 1024 * 1024
    elif unit == 'GB':
        size_b = size * 1024 * 1024 * 1024
    elif unit == 'TB':
        size_b = size * 1024 * 1024 * 1024 * 1024
    else:
        raise ValueError(f"Unsupported size unit {item['name']} {item['size']} {item['sizeunit']}")

    size_mb = size_b / (1024 * 1024)
    return int(size_mb)


def sanitize_title(title: str) -> str:
    umlaut_map = {
        "Ä": "Ae", "ä": "ae",
        "Ö": "Oe", "ö": "oe",
        "Ü": "Ue", "ü": "ue",
        "ß": "ss"
    }
    for umlaut, replacement in umlaut_map.items():
        title = title.replace(umlaut, replacement)

    title = title.encode("ascii", errors="ignore").decode()

    # Replace slashes and spaces with dots
    title = title.replace("/", "").replace(" ", ".")
    title = title.strip(".")  # no leading/trailing dots
    title = title.replace(".-.", "-")  # .-. → -

    # Finally, drop any chars except letters, digits, dots, hyphens, ampersands
    title = re.sub(r"[^A-Za-z0-9.\-&]", "", title)

    # remove any repeated dots
    title = re.sub(r"\.{2,}", ".", title)
    return title


def sanitize_string(s):
    s = s.lower()

    # Remove dots / pluses
    s = s.replace('.', ' ')
    s = s.replace('+', ' ')
    s = s.replace('_', ' ')
    s = s.replace('-', ' ')

    # Umlauts
    s = re.sub(r'ä', 'ae', s)
    s = re.sub(r'ö', 'oe', s)
    s = re.sub(r'ü', 'ue', s)
    s = re.sub(r'ß', 'ss', s)

    # Remove special characters
    s = re.sub(r'[^a-zA-Z0-9\s]', '', s)

    # Remove season and episode patterns
    s = re.sub(r'\bs\d{1,3}(e\d{1,3})?\b', '', s)

    # Remove German and English articles
    articles = r'\b(?:der|die|das|ein|eine|einer|eines|einem|einen|the|a|an|and)\b'
    s = re.sub(articles, '', s, re.IGNORECASE)

    # Replace obsolete titles
    s = s.replace('navy cis', 'ncis')

    # Remove extra whitespace
    s = ' '.join(s.split())

    return s


def search_string_in_sanitized_title(search_string, title):
    sanitized_search_string = sanitize_string(search_string)
    sanitized_title = sanitize_string(title)

    # Use word boundaries to ensure full word/phrase match
    if re.search(rf'\b{re.escape(sanitized_search_string)}\b', sanitized_title):
        debug(f"Matched search string: {sanitized_search_string} with title: {sanitized_title}")
        return True
    else:
        debug(f"Skipping {title} as it doesn't match search string: {sanitized_search_string}")
        return False


def is_imdb_id(search_string):
    if bool(re.fullmatch(r"tt\d{7,}", search_string)):
        return search_string
    else:
        return None


def match_in_title(title: str, season: int = None, episode: int = None) -> bool:
    # ensure season/episode are ints (or None)
    if isinstance(season, str):
        try:
            season = int(season)
        except ValueError:
            season = None
    if isinstance(episode, str):
        try:
            episode = int(episode)
        except ValueError:
            episode = None

    pattern = re.compile(
        r"(?i)(?:\.|^)[sS](\d+)(?:-(\d+))?"  # season or season‑range
        r"(?:[eE](\d+)(?:-(?:[eE]?)(\d+))?)?"  # episode or episode‑range
        r"(?=[\.-]|$)"
    )

    matches = pattern.findall(title)
    if not matches:
        return False

    for s_start, s_end, e_start, e_end in matches:
        se_start, se_end = int(s_start), int(s_end or s_start)

        # if a season was requested, ensure it falls in the range
        if season is not None and not (se_start <= season <= se_end):
            continue

        # if no episode requested, only accept if the title itself had no episode tag
        if episode is None:
            if not e_start:
                return True
            else:
                # title did specify an episode — skip this match
                continue

        # episode was requested, so title must supply one
        if not e_start:
            continue

        ep_start, ep_end = int(e_start), int(e_end or e_start)
        if ep_start <= episode <= ep_end:
            return True

    return False


def is_valid_release(title: str,
                     request_from: str,
                     search_string: str,
                     season: int = None,
                     episode: int = None) -> bool:
    """
    Return True if the given release title is valid for the given search parameters.
    - title: the release title to test
    - request_from: user agent, contains 'Radarr' for movie searches or 'Sonarr' for TV searches
    - search_string: the original search phrase (could be an IMDb id or plain text)
    - season: desired season number (or None)
    - episode: desired episode number (or None)
    """
    try:
        # if search string is NOT an imdb id check search_string_in_sanitized_title - if not match, its not valid
        if not is_imdb_id(search_string):
            if not search_string_in_sanitized_title(search_string, title):
                debug(f"Skipping {title!r} as it doesn't match sanitized search string: {search_string!r}")
                return False

        # Determine whether this is a movie or TV search
        rf = request_from.lower()
        is_movie_search = 'radarr' in rf
        is_tv_search = 'sonarr' in rf

        # if it's a movie search, don't allow any TV show titles (check for NO season or episode tags in the title)
        if is_movie_search:
            if not MOVIE_REGEX.match(title):
                debug(f"Skipping {title!r} as title doesn't match movie regex: {MOVIE_REGEX.pattern}")
                return False
            return True

        # if it's a TV show search, don't allow any movies (check for season or episode tags in the title)
        if is_tv_search:
            # must have some S/E tag present
            if not SEASON_EP_REGEX.search(title):
                debug(f"Skipping {title!r} as title doesn't match TV show regex: {SEASON_EP_REGEX.pattern}")
                return False
            # if caller specified a season or episode, double‑check the match
            if season is not None or episode is not None:
                if not match_in_title(title, season, episode):
                    debug(f"Skipping {title!r} as it doesn't match season {season} and episode {episode}")
                    return False
            return True

        # unknown search source — reject by default
        debug(f"Skipping {title!r} as search source is unknown: {request_from!r}")
        return False

    except Exception as e:
        # log exception message and short stack trace
        tb = traceback.format_exc()
        debug(f"Exception in is_valid_release: {e!r}\n{tb}"
              f"is_valid_release called with "
              f"title={title!r}, request_from={request_from!r}, "
              f"search_string={search_string!r}, season={season!r}, episode={episode!r}")
        return False


def get_recently_searched(shared_state, context, timeout_seconds):
    recently_searched = shared_state.values.get(context, {})
    threshold = datetime.now() - timedelta(seconds=timeout_seconds)
    keys_to_remove = [key for key, value in recently_searched.items() if value["timestamp"] <= threshold]
    for key in keys_to_remove:
        debug(f"Removing '{key}' from recently searched memory ({context})...")
        del recently_searched[key]
    return recently_searched


def download_package(links, title, password, package_id):
    device = get_device()
    downloaded = device.linkgrabber.add_links(params=[
        {
            "autostart": False,
            "links": json.dumps(links),
            "packageName": title,
            "extractPassword": password,
            "priority": "DEFAULT",
            "downloadPassword": password,
            "destinationFolder": "Quasarr/<jd:packagename>",
            "comment": package_id,
            "overwritePackagizerRules": True
        }
    ])
    return downloaded
