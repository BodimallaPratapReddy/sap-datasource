import base64
import logging
from os import environ
from pathlib import Path
from urllib.parse import urlparse

import httpx
from dotenv import load_dotenv

logger = logging.getLogger(__name__)

PROJECT_ENV_FILE = Path(__file__).resolve().parents[2] / ".env"
load_dotenv(PROJECT_ENV_FILE)


def _rest_config(url_key: str):
    url = (environ.get(url_key) or "").strip()
    user = environ.get("SAP_TABLEDATA_USER")
    password = environ.get("SAP_TABLEDATA_PASSWORD")
    sap_client = environ.get("SAP_CLIENT", "020")

    missing = [key for key, value in ((url_key, url), ("SAP_TABLEDATA_USER", user), ("SAP_TABLEDATA_PASSWORD", password)) if not value]
    if missing:
        logger.error("Missing required environment variable(s) in %s: %s", PROJECT_ENV_FILE, ", ".join(missing))
        return None

    parsed_url = urlparse(url)
    if parsed_url.scheme not in ("http", "https") or not parsed_url.hostname:
        logger.error("%s must be a valid HTTP(S) URL in %s", url_key, PROJECT_ENV_FILE)
        return None

    return url, user, password, sap_client


async def fetch_table_data_rest(tablename: str, filters=None, output_fields=None, top: int = 1000, skip: int = 0):
    config = _rest_config("SAP_TABLEDATA_URL")
    if config is None:
        return None
    url, user, password, sap_client = config
    auth_token = base64.b64encode(f"{user}:{password}".encode()).decode()
    body = {
        'tablename': tablename,
        'filters': filters or [],
        'outputFields': output_fields or [],
        'top': top,
        'skip': skip,
    }
    logger.info(f"API CALL z_tabledata POST {url}?sap-client={sap_client} payload={body}")
    try:
        async with httpx.AsyncClient(timeout=30.0) as client:
            resp = await client.post(
                url,
                params={'sap-client': sap_client},
                headers={'Authorization': f'Basic {auth_token}', 'Content-Type': 'application/json'},
                json=body,
            )
        resp.raise_for_status()
        data = resp.json()
    except httpx.ConnectError as e:
        logger.warning("SAP REST table-read connection failed for %s (host=%s): %r", tablename, urlparse(url).hostname, e)
        return None
    except Exception as e:
        logger.warning(f"SAP REST table-read fallback failed for {tablename}: {e!r}")
        return None
    if isinstance(data, list):
        logger.info(f"API RESULT z_tabledata {tablename}: {len(data)} row(s)")
        return data
    if isinstance(data, dict):
        for key in ('values', 'result', 'data', 'rows'):
            if isinstance(data.get(key), list):
                logger.info(f"API RESULT z_tabledata {tablename}: {len(data[key])} row(s)")
                return data[key]
        logger.info(f"API RESULT z_tabledata {tablename}: 0 row(s) (unrecognized response shape: {list(data.keys())})")
        return []
    return []


async def fetch_function_source_rest(function_name: str):
    config = _rest_config("SAP_FUNCTIONSRC_URL")
    if config is None:
        return None
    url, user, password, sap_client = config
    auth_token = base64.b64encode(f"{user}:{password}".encode()).decode()
    payload = {'functionName': function_name, 'mcpMode': False}
    logger.info(f"API CALL z_functionsrc POST {url}?sap-client={sap_client} payload={payload}")
    try:
        async with httpx.AsyncClient(timeout=30.0) as client:
            resp = await client.post(
                url,
                params={'sap-client': sap_client},
                headers={'Authorization': f'Basic {auth_token}', 'Content-Type': 'application/json'},
                json=payload,
            )
        resp.raise_for_status()
        data = resp.json()
    except httpx.ConnectError as e:
        logger.warning("SAP REST function-source connection failed for %s (host=%s): %r", function_name, urlparse(url).hostname, e)
        return None
    except Exception as e:
        logger.warning(f"SAP REST function-source fallback failed for {function_name}: {e!r}")
        return None
    if isinstance(data, str):
        logger.info(f"API RESULT z_functionsrc {function_name}: {len(data)} char(s)")
        return data
    if isinstance(data, dict):
        for key in ('source', 'sourceCode', 'sourcecode', 'functionSource', 'code', 'abapSource'):
            val = data.get(key)
            if isinstance(val, str) and val:
                logger.info(f"API RESULT z_functionsrc {function_name}: {len(val)} char(s) (key={key})")
                return val
        logger.info(f"API RESULT z_functionsrc {function_name}: no source found (unrecognized response shape: {list(data.keys())})")
    return None


async def fetch_infoset_rest(infoset_name: str):
    config = _rest_config("SAP_INFOSET_URL")
    if config is None:
        return None
    url, user, password, sap_client = config
    auth_token = base64.b64encode(f"{user}:{password}".encode()).decode()
    logger.info(
        "API CALL z_infoset POST %s infoset=%s sap-client=%s",
        urlparse(url).hostname,
        infoset_name,
        sap_client,
    )
    try:
        async with httpx.AsyncClient(timeout=30.0) as client:
            resp = await client.post(
                url,
                params={'sap-client': sap_client, 'infoset': infoset_name},
                headers={'Authorization': f'Basic {auth_token}'},
                content=b'',
            )
        resp.raise_for_status()
        data = resp.json()
    except httpx.ConnectError as e:
        logger.warning(
            "SAP REST InfoSet connection failed for %s (host=%s): %r",
            infoset_name,
            urlparse(url).hostname,
            e,
        )
        return None
    except Exception as e:
        logger.warning("SAP REST InfoSet lookup failed for %s: %r", infoset_name, e)
        return None
    if not isinstance(data, dict):
        logger.warning("SAP REST InfoSet lookup returned an unexpected response for %s", infoset_name)
        return None
    return data
