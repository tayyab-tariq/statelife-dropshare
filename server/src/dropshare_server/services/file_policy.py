"""Banned file types (FR-SAFE-02, OQ-56, OQ-57): the checks every write that names a file runs.

A refusal rolls back the request's transaction first, which ends its locks and returns its
connection to the pool, and then audits in a transaction of its own. Auditing while the request
still held its connection would need two at once: a burst of refusals could take the whole pool.
"""

from collections.abc import Iterable, Sequence
from dataclasses import dataclass

from sqlalchemy.orm import Session, sessionmaker

from dropshare_server.core.errors import AppError
from dropshare_server.models import Node
from dropshare_server.repositories import nodes as node_repository
from dropshare_server.services import audit as audit_service
from dropshare_server.services.audit import AuditTarget, node_target
from dropshare_server.services.context import CallContext
from dropshare_server.services.file_types import ExecutableKind
from dropshare_shared.errors import ErrorCode
from dropshare_shared.naming import file_extension
from dropshare_shared.schemas.audit import AuditAction, AuditOutcome
from dropshare_shared.schemas.error_details import FileTypeNotAllowedDetails, FileTypeProblem
from dropshare_shared.schemas.nodes import NodeKind

_NAMES_SHOWN = 10  # banned files named in a folder refusal


@dataclass(frozen=True)
class FileTypePolicy:
    banned: frozenset[str]  # in `naming.normalize_extension` form
    session_factory: sessionmaker[Session]  # where refusals are audited

    def banned_extension(self, name: str) -> str | None:
        extension = file_extension(name)
        return extension if extension is not None and extension in self.banned else None

    def banned_files(self, items: Iterable[Node]) -> list[Node]:
        """The files among `items` whose name has a banned extension."""
        return [
            item
            for item in items
            if item.kind == NodeKind.FILE
            and item.name is not None
            and self.banned_extension(item.name) is not None
        ]

    def check_name(
        self, session: Session, ctx: CallContext, name: str, target: AuditTarget
    ) -> None:
        """Refuse a file named with a banned last extension."""
        extension = self.banned_extension(name)
        if extension is not None:
            raise self.refuse_extension(session, ctx, target, extension)

    def check_folder(self, session: Session, ctx: CallContext, folder: Node) -> None:
        """Refuse moving, renaming or sharing a folder that holds files of a banned type
        (OQ-57)."""
        names = sorted(
            node_repository.live_file_names_ending_with(
                session, folder.id, self.banned, _NAMES_SHOWN
            )
        )
        if names:
            raise self.refuse(
                session,
                ctx,
                node_target(folder),
                FileTypeProblem.CONTAINS_BANNED_FILE,
                f"This folder holds a file of a type DropShare doesn't allow ({names[0]}). "
                "Rename or delete that file first.",
                file_names=names,
            )

    def refuse_extension(
        self, session: Session, ctx: CallContext, target: AuditTarget, extension: str
    ) -> AppError:
        return self.refuse(
            session,
            ctx,
            target,
            FileTypeProblem.BANNED_EXTENSION,
            f"Files of type {extension} aren't allowed in DropShare.",
            extension=extension,
        )

    def refuse_executable(
        self, session: Session, ctx: CallContext, target: AuditTarget, kind: ExecutableKind
    ) -> AppError:
        return self.refuse(
            session,
            ctx,
            target,
            FileTypeProblem.EXECUTABLE_CONTENT,
            "This file is a program, and DropShare doesn't allow programs, whatever their name.",
            audit_details={"executable": kind.value},
        )

    def refuse(
        self,
        session: Session,
        ctx: CallContext,
        target: AuditTarget,
        problem: FileTypeProblem,
        message: str,
        *,
        extension: str | None = None,
        file_names: Sequence[str] = (),
        audit_details: dict[str, str | int] | None = None,
    ) -> AppError:
        """Roll back the request, audit `file.type_blocked`, and return the 422 to raise."""
        session.rollback()
        audit_service.record_now(
            self.session_factory,
            ctx,
            AuditAction.FILE_TYPE_BLOCKED,
            AuditOutcome.DENIED,
            target=target,
            details={
                "reason": problem.value,
                "extension": extension,
                **(
                    {"file_name": file_names[0], "files_shown": len(file_names)}
                    if file_names
                    else {}
                ),
                **(audit_details or {}),
            },
        )
        details = FileTypeNotAllowedDetails(
            reason=problem, extension=extension, file_names=list(file_names)
        )
        return AppError(
            422, ErrorCode.FILE_TYPE_NOT_ALLOWED, message, details.model_dump(), audited=True
        )
