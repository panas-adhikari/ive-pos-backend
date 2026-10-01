# Only implemented capabilities can be granted. Roles are editable permission templates.
PLATFORM_ROLES = {"none", "super_admin", "employee"}
PLATFORM_PERMISSIONS = {
    "super_admin": [
        "platform.organizations.manage",
        "platform.support.manage",
        "platform.users.manage",
    ],
    "employee": [
        "platform.organizations.read",
        "platform.onboarding.manage",
        "platform.support.manage",
    ],
}
OWNER_PERMISSIONS = [
    "organization.read",
    "organization.setup",
    "employees.manage",
    "store.read",
    "catalog.manage",
    "inventory.manage",
    "sales.create",
    "reports.read",
]
ORGANIZATION_PERMISSIONS = {"organization.setup", "employees.manage"}
ROLE_PERMISSIONS = {
    "owner": OWNER_PERMISSIONS,
    "organization_admin": OWNER_PERMISSIONS,
    "administrator": OWNER_PERMISSIONS,  # Legacy alias retained for existing memberships.
    "store_admin": [
        "organization.read",
        "store.read",
        "catalog.manage",
        "inventory.manage",
        "sales.create",
        "reports.read",
    ],
    "store_manager": [
        "organization.read",
        "store.read",
        "catalog.manage",
        "inventory.manage",
        "sales.create",
        "reports.read",
    ],
    "cashier": ["organization.read", "store.read", "sales.create"],
    "inventory_manager": [
        "organization.read",
        "store.read",
        "catalog.manage",
        "inventory.manage",
        "reports.read",
    ],
    "accountant": ["organization.read", "store.read", "reports.read"],
}

# Default store scope for role templates. Staff access can still be tailored per person.
ROLE_DEFAULT_ALL_STORES = {"owner", "organization_admin", "administrator"}
