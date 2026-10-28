"""Audit event vocabulary (FR-AUDIT-01/02). Values are stored, so never rename one."""

from enum import StrEnum


class AuditAction(StrEnum):
    AUTH_LOGIN = "auth.login"
    AUTH_LOGOUT = "auth.logout"
    AUTH_REFRESH_REUSE = "auth.refresh_reuse"
    AUTH_LOCKED = "auth.locked"
    AUTH_UNLOCKED = "auth.unlocked"
    AUTH_PASSWORD_CHANGED = "auth.password_changed"  # noqa: S105 - action name, not a secret
    AUTH_PASSWORD_RESET = "auth.password_reset"  # noqa: S105 - action name, not a secret
    USER_CREATED = "user.created"
    USER_DISABLED = "user.disabled"
    USER_ENABLED = "user.enabled"
    USER_LISTED = "user.listed"
    AUDIT_EXPORTED = "audit.exported"
    FOLDER_CREATED = "folder.created"
    FILE_UPLOADED = "file.uploaded"
    FILE_DOWNLOADED = "file.downloaded"
    NODE_TRASHED = "node.trashed"
    NODE_RENAMED = "node.renamed"
    NODE_MOVED = "node.moved"
    SHARE_CREATED = "share.created"
    SHARE_CHANGED = "share.changed"
    SHARE_REMOVED = "share.removed"
    SHARE_ACCEPTED = "share.accepted"
    SHARE_LEFT = "share.left"
    GROUP_CREATED = "group.created"
    GROUP_DELETED = "group.deleted"
    GROUP_MEMBER_ADDED = "group.member_added"
    GROUP_MEMBER_REMOVED = "group.member_removed"
    GROUP_LISTED = "group.listed"
    ACCESS_DENIED = "access.denied"


class AuditOutcome(StrEnum):
    SUCCESS = "success"
    FAILURE = "failure"
    DENIED = "denied"


class AuditActorType(StrEnum):
    USER = "user"
    CLI = "cli"
    ANONYMOUS = "anonymous"
