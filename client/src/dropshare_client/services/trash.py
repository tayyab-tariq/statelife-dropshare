"""The trash on the server (FR-TRASH-01 … FR-TRASH-03): what was deleted, putting it back, and
deleting it for good. Items in the trash aren't on this computer, so these go to the server
directly; what comes back reaches the sync folder through ordinary sync. Blocking calls:
controllers run them on the pool."""

import uuid

from dropshare_client.services.api_client import ApiClient
from dropshare_client.services.errors import ApiError
from dropshare_shared.errors import ErrorCode
from dropshare_shared.schemas.envelope import PAGE_LIMIT_MAX, ListResponse
from dropshare_shared.schemas.nodes import NodeOut
from dropshare_shared.schemas.trash import EmptyTrashOut, TrashItemOut


class Trash:
    def __init__(self, api: ApiClient) -> None:
        self._api = api

    def items(self) -> list[TrashItemOut]:
        """Everything in the user's trash, most recently deleted first, page by page."""
        items: list[TrashItemOut] = []
        page = 1
        while True:
            response = self._api.envelope(
                "GET",
                "/api/v1/trash",
                ListResponse[TrashItemOut],
                params={"page": page, "limit": PAGE_LIMIT_MAX},
            )
            items.extend(response.data)
            if not response.data or len(items) >= response.meta.total:
                return _distinct(items)
            page += 1

    def restore(self, node_id: uuid.UUID) -> NodeOut:
        """Safe to repeat: once restored, the server answers with the item as it is."""
        return self._api.data("POST", f"/api/v1/trash/{node_id}/restore", NodeOut)

    def delete_forever(self, node_id: uuid.UUID) -> None:
        """Already gone counts as done: deleted permanently by this request's first attempt, or
        by someone else. Restored meanwhile is an error (409) and says so."""
        try:
            self._api.no_content("DELETE", f"/api/v1/trash/{node_id}")
        except ApiError as exc:
            if exc.code != ErrorCode.NOT_FOUND:
                raise

    def empty(self) -> EmptyTrashOut:
        return self._api.data("DELETE", "/api/v1/trash", EmptyTrashOut)


def _distinct(items: list[TrashItemOut]) -> list[TrashItemOut]:
    """Pages are read one after another: an item deleted meanwhile shifts the later ones, so
    the same item can come twice."""
    distinct: dict[uuid.UUID, TrashItemOut] = {}
    for item in items:
        distinct.setdefault(item.id, item)
    return list(distinct.values())
