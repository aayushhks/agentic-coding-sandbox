"""A small async client for the Docker Engine API, over the daemon's unix socket."""

import json
import struct
from collections.abc import Iterator
from typing import Any, cast

import httpx

API_VERSION = "v1.44"
DEFAULT_SOCKET = "/var/run/docker.sock"


class DockerError(Exception):
    """The daemon refused a request, or answered with an error."""

    def __init__(self, status: int, message: str) -> None:
        super().__init__(f"docker answered {status}: {message}")
        self.status = status


STDOUT, STDERR = 1, 2


def _frames(raw: bytes) -> Iterator[tuple[int, bytes]]:
    """The frames of a non-tty log stream: an 8-byte header naming the stream, then a payload."""
    at = 0
    while at + 8 <= len(raw):
        stream, size = struct.unpack(">BxxxI", raw[at : at + 8])
        yield stream, raw[at + 8 : at + 8 + size]
        at += 8 + size


def _demux(raw: bytes, streams: tuple[int, ...] = (STDOUT, STDERR)) -> str:
    """What a log stream holds from the given streams, in the order it was written."""
    return b"".join(data for stream, data in _frames(raw) if stream in streams).decode(
        errors="replace"
    )


class Docker:
    def __init__(self, socket_path: str = DEFAULT_SOCKET) -> None:
        self._http = httpx.AsyncClient(
            transport=httpx.AsyncHTTPTransport(uds=socket_path),
            base_url=f"http://docker/{API_VERSION}",
            timeout=httpx.Timeout(60.0),
        )

    async def _call(
        self,
        method: str,
        path: str,
        *,
        body: dict[str, Any] | None = None,
        params: dict[str, str] | None = None,
        seconds: float | None = 60.0,
        allow: tuple[int, ...] = (),
    ) -> httpx.Response:
        response = await self._http.request(method, path, json=body, params=params, timeout=seconds)
        if response.status_code >= 400 and response.status_code not in allow:
            try:
                message = str(response.json().get("message", response.text))
            except ValueError:
                message = response.text
            raise DockerError(response.status_code, message)
        return response

    async def ping(self) -> bool:
        try:
            return (await self._call("GET", "/_ping")).text == "OK"
        except (httpx.TransportError, DockerError):
            return False

    async def image(self, image: str) -> dict[str, Any] | None:
        """What the daemon knows about an image, or None when it doesn't have it."""
        response = await self._call("GET", f"/images/{image}/json", allow=(404,))
        if response.status_code == 404:
            return None
        return cast(dict[str, Any], response.json())

    async def image_id(self, image: str) -> str | None:
        """The image's content digest, or None when the daemon doesn't have it."""
        found = await self.image(image)
        return None if found is None else str(found["Id"])

    async def create(self, name: str, config: dict[str, Any]) -> str:
        response = await self._call(
            "POST", "/containers/create", body=config, params={"name": name}
        )
        return str(response.json()["Id"])

    async def start(self, container: str) -> None:
        await self._call("POST", f"/containers/{container}/start")

    async def wait(self, container: str) -> int:
        """Block until the container stops, and return its exit code."""
        response = await self._call("POST", f"/containers/{container}/wait", seconds=None)
        return int(response.json()["StatusCode"])

    async def inspect(self, container: str) -> dict[str, Any]:
        return cast(
            dict[str, Any], (await self._call("GET", f"/containers/{container}/json")).json()
        )

    async def logs(self, container: str, *, tail: int = 200) -> str:
        response = await self._call(
            "GET",
            f"/containers/{container}/logs",
            params={"stdout": "1", "stderr": "1", "tail": str(tail)},
        )
        return _demux(response.content)

    async def output(self, container: str) -> tuple[str, str]:
        """Everything the container wrote, as its stdout and its stderr."""
        response = await self._call(
            "GET", f"/containers/{container}/logs", params={"stdout": "1", "stderr": "1"}
        )
        return _demux(response.content, (STDOUT,)), _demux(response.content, (STDERR,))

    async def kill(self, container: str) -> None:
        # 409: it had already stopped, which is what a kill wants anyway
        await self._call("POST", f"/containers/{container}/kill", allow=(404, 409))

    async def remove(self, container: str) -> None:
        await self._call(
            "DELETE", f"/containers/{container}", params={"force": "true"}, allow=(404, 409)
        )

    async def containers(self, labels: dict[str, str]) -> list[dict[str, Any]]:
        """Every container, running or not, that carries all of these labels."""
        filters = json.dumps({"label": [f"{key}={value}" for key, value in labels.items()]})
        response = await self._call(
            "GET", "/containers/json", params={"all": "true", "filters": filters}
        )
        return cast(list[dict[str, Any]], response.json())

    async def stats(self, container: str) -> dict[str, Any]:
        response = await self._call(
            "GET", f"/containers/{container}/stats", params={"stream": "false"}
        )
        return cast(dict[str, Any], response.json())

    async def create_network(self, name: str, *, internal: bool, labels: dict[str, str]) -> str:
        body = {"Name": name, "Driver": "bridge", "Internal": internal, "Labels": labels}
        return str((await self._call("POST", "/networks/create", body=body)).json()["Id"])

    async def connect(self, network: str, container: str, *, aliases: list[str]) -> None:
        body = {"Container": container, "EndpointConfig": {"Aliases": aliases}}
        await self._call("POST", f"/networks/{network}/connect", body=body)

    async def remove_network(self, network: str) -> None:
        await self._call("DELETE", f"/networks/{network}", allow=(404,))

    async def networks(self, labels: dict[str, str]) -> list[dict[str, Any]]:
        filters = json.dumps({"label": [f"{key}={value}" for key, value in labels.items()]})
        response = await self._call("GET", "/networks", params={"filters": filters})
        return cast(list[dict[str, Any]], response.json())

    async def aclose(self) -> None:
        await self._http.aclose()
