# libflatpak-js

JavaScript bindings for libflatpak, providing full access to Flatpak installation management, remote repositories, application installation, and system information from Node.js.

## Features

- **Full API Coverage**: All 11 Flatpak GObject classes with methods and properties
- **Automatic Type Conversion**: GPtrArray → JavaScript arrays with element wrapping
- **Nullable Parameter Support**: Optional parameters accept `null`/`undefined`
- **Memory Management**: Automatic cleanup of native resources
- **Inheritance Support**: JavaScript classes reflect GObject inheritance hierarchy

## Installation

```bash
npm install libflatpak
```

**Prerequisites:**

- Flatpak development libraries (`flatpak-devel` package on Fedora/RHEL, `libflatpak-dev` on Debian/Ubuntu)
- Node.js 14 or later
- C++ build tools (for native module compilation)

## Quick Start

### Basic System Information

```javascript
const { getDefaultArch, getSystemInstallations } = require("libflatpak");

// Get system information
const arch = getDefaultArch();
console.log(`Default architecture: ${arch}`);

// Get available installations
const installations = getSystemInstallations();
console.log(`Found ${installations.length} installation(s)`);

if (installations.length > 0) {
    const installation = installations[0];
    console.log(`Installation ID: ${installation.getId()}`);
    console.log(`Is user installation: ${installation.getIsUser()}`);
}
```

### Working with Installations

```javascript
const { getSystemInstallations } = require("libflatpak");

// Get available installations
const installations = getSystemInstallations();
console.log(`Found ${installations.length} installation(s)`);

if (installations.length === 0) {
    console.error("No system installation found");
    process.exit(1);
}

const installation = installations[0];

// List installed applications
console.log("\nListing installed applications...");
try {
    const installedRefs = installation.listInstalledRefs();
    console.log(`Found ${installedRefs.length} installed applications:`);

    installedRefs.forEach((ref, index) => {
        console.log(`${index + 1}. ${ref.getName()} (${ref.getKind()})`);
    });
} catch (error) {
    console.error(`Error listing installed refs: ${error.message}`);
}

// List available remotes
console.log("\nListing available remotes...");
try {
    const remotes = installation.listRemotes();
    console.log(`Found ${remotes.length} remotes:`);

    remotes.forEach((remote) => {
        console.log(`- ${remote.getName()}: ${remote.getUrl()}`);
    });
} catch (error) {
    console.error(`Error listing remotes: ${error.message}`);
}
```

## Advanced Examples

### Example 1: Adding a Remote Repository

```javascript
const { getSystemInstallation, Remote } = require("libflatpak");

async function addFlathubRemote() {
    const installation = getSystemInstallation();
    if (!installation) {
        throw new Error("No system installation available");
    }

    try {
        // Create a new remote object
        const remote = Remote.create("flathub");

        // Configure the remote
        remote.setUrl("https://dl.flathub.org/repo/");
        remote.setTitle("Flathub");
        remote.setComment("The central repository for Flatpak applications");
        remote.setGpgVerify(true);
        remote.setNoenumerate(false);
        remote.setDisabled(false);

        // Add the remote to the installation
        const added = installation.addRemote(remote, false, null);
        if (added) {
            console.log("Successfully added Flathub remote");

            // Update remote metadata
            const updated = installation.updateRemoteSync("flathub", null);
            if (updated) {
                console.log("Remote metadata updated successfully");
            }
        } else {
            console.log("Remote already exists or addition failed");
        }

        remote.free();
    } finally {
        installation.free();
    }
}

addFlathubRemote().catch(console.error);
```

### Example 2: Installing a Package

```javascript
const { getSystemInstallation, Transaction } = require("libflatpak");

async function installApplication(appId) {
    const installation = getSystemInstallation();
    if (!installation) {
        throw new Error("No system installation available");
    }

    try {
        // Download the .flatpakref file
        const flatpakrefUrl = `https://dl.flathub.org/repo/appstream/${appId}.flatpakref`;
        console.log(`Downloading ${flatpakrefUrl}...`);

        const flatpakrefData = await fetch(flatpakrefUrl).then(res => res.bytes());

        // Create a transaction for this installation
        const transaction = Transaction.create(installation, null);

        // Configure transaction options
        transaction.setNoInteraction(false);
        transaction.setAutoInstallSdk(true);

        // Add the application to install
        // flatpakrefData is a Buffer containing the .flatpakref file
        const added = transaction.addInstallFlatpakref(flatpakrefData);

        if (!added) {
            throw new Error(`Failed to add installation for ${appId}`);
        }

        console.log(`Starting installation of ${appId}...`);

        // Run the transaction
        const success = transaction.run(null);

        if (success) {
            console.log(`Successfully installed ${appId}`);

            // Get the installed ref
            const installedRef = installation.getInstalledRef(
                0, // FLATPAK_REF_KIND_APP
                appId,
                "x86_64",
                "stable",
                null,
            );

            if (installedRef) {
                console.log(
                    `Installed version: ${installedRef.getAppdataVersion()}`,
                );
                installedRef.free();
            }
        } else {
            console.error(`Failed to install ${appId}`);
        }

        transaction.free();
    } finally {
        installation.free();
    }
}

// Alternative: Install from a local .flatpakref file
async function installFromLocalFile(filePath) {
    const { readFileSync } = require("fs");
    const installation = getSystemInstallation();
    if (!installation) {
        throw new Error("No system installation available");
    }

    try {
        // Read local .flatpakref file
        const flatpakrefData = readFileSync(filePath);

        const transaction = Transaction.create(installation, null);
        transaction.setNoInteraction(false);

        const added = transaction.addInstallFlatpakref(flatpakrefData);

        if (added) {
            const success = transaction.run(null);
            if (success) {
                console.log("Installation from local file completed");
            }
        }

        transaction.free();
    } finally {
        installation.free();
    }
}

// Install a sample application
// installApplication('org.gnome.Calculator').catch(console.error);
// Or install from local file: installFromLocalFile('/path/to/app.flatpakref').catch(console.error);
```

### Example 3: Getting App Data for Store

```javascript
const { getSystemInstallation } = require("libflatpak");

async function getAppStoreData() {
    const installation = getSystemInstallation();
    if (!installation) {
        throw new Error("No system installation available");
    }

    try {
        // Get all installed applications
        const installedRefs = installation.listInstalledRefs();

        const appData = [];

        for (const ref of installedRefs) {
            // Only process applications (not runtimes)
            if (ref.getKind() === 0) {
                // FLATPAK_REF_KIND_APP
                const appInfo = {
                    id: ref.getName(),
                    name: ref.getAppdataName() || ref.getName(),
                    summary: ref.getAppdataSummary() || "",
                    version: ref.getAppdataVersion() || "Unknown",
                    license: ref.getAppdataLicense() || "",
                    installSize: ref.getInstalledSize(),
                    isCurrent: ref.getIsCurrent(),
                    origin: ref.getOrigin(),
                };

                // Try to load full appdata (may fail if not available)
                try {
                    const appdataBytes = ref.loadAppdata(null);
                    if (appdataBytes && appdataBytes.length > 0) {
                        appInfo.hasFullAppdata = true;
                        // appdataBytes is a Buffer containing the appdata XML
                        // You could parse it with an XML parser for more details
                    }
                } catch (err) {
                    // Appdata not available or failed to load
                    appInfo.hasFullAppdata = false;
                }

                appData.push(appInfo);

                console.log(
                    `${appInfo.name} v${appInfo.version}: ${appInfo.summary}`,
                );
            }
        }

        console.log(`\nTotal applications: ${appData.length}`);
        return appData;
    } finally {
        installation.free();
    }
}

// Also get available applications from remotes
async function getAvailableAppsFromRemote(remoteName) {
    const installation = getSystemInstallation();
    if (!installation) {
        throw new Error("No system installation available");
    }

    try {
        // List remote refs (available applications)
        const remoteRefs = installation.listRemoteRefsSync(remoteName, null);

        console.log(`Available applications in ${remoteName}:`);
        remoteRefs.forEach((ref) => {
            if (ref.getKind() === 0) {
                // FLATPAK_REF_KIND_APP
                console.log(
                    `- ${ref.getName()} (${ref.getDownloadSize()} bytes download)`,
                );
            }
        });

        return remoteRefs;
    } finally {
        installation.free();
    }
}

// Usage
getAppStoreData()
    .then((apps) => {
        console.log(`Found ${apps.length} installed applications`);
    })
    .catch(console.error);

// Uncomment to also check remote applications
// getAvailableAppsFromRemote('flathub').catch(console.error);
```

## API Reference

### Top-level Functions

| Function                                        | Description                                  |
| ----------------------------------------------- | -------------------------------------------- |
| `getDefaultArch(): string`                      | Default system architecture (e.g., "x86_64") |
| `getSupportedArches(): string[]`                | Array of supported architectures             |
| `getSystemInstallations(): Installation[]`      | All available system installations           |
| `getSystemInstallation(): Installation \| null` | First system installation, if any            |
| `errorQuark(): number`                          | Flatpak error domain quark                   |
| `portalErrorQuark(): number`                    | Flatpak portal error domain quark            |
| `parse(ref: string): Ref`                       | Parse a Flatpak ref string                   |
| `toString(kind: number): string`                | Convert operation type to string             |
| `getAll(): Instance[]`                          | Get all running Flatpak instances            |

### Core Classes

#### `Installation`

Represents a Flatpak installation (system or user).

**Key Methods:**

- `getId(): string` - Installation identifier
- `getPath(): string` - Filesystem path
- `getIsUser(): boolean` - True for user installations
- `listInstalledRefs(cancellable?): InstalledRef[]` - List installed applications
- `listRemotes(cancellable?): Remote[]` - List configured remotes
- `addRemote(remote, if_needed, cancellable?): boolean` - Add a remote
- `getRemoteByName(name, cancellable?): Remote` - Get remote by name
- `updateRemoteSync(name, cancellable?): boolean` - Update remote metadata

#### `Remote`

Represents a remote repository.

**Key Methods:**

- `getName(): string` - Remote name
- `getUrl(): string` - Repository URL
- `setUrl(url): void` - Set repository URL
- `setGpgVerify(verify): void` - Enable/disable GPG verification
- `setTitle(title): void` - Set human-readable title
- `setDisabled(disabled): void` - Enable/disable remote

#### `Transaction`

Manages installation/removal operations.

**Key Methods:**

- `addInstallFlatpakref(flatpakref_data): boolean` - Install from .flatpakref
- `addInstallBundle(file, gpg_data, cancellable?): boolean` - Install from bundle
- `addUninstall(ref, cancellable?): boolean` - Uninstall application
- `run(cancellable?): boolean` - Execute transaction
- `setNoInteraction(no_interaction): void` - Disable user interaction
- `setAutoInstallSdk(auto_install_sdk): void` - Auto-install SDK

#### `InstalledRef`

Represents an installed application or runtime.

**Key Methods:**

- `getName(): string` - Application ID
- `getKind(): number` - Ref kind (app/runtime)
- `getAppdataName(): string` - Application name from appdata
- `getAppdataSummary(): string` - Short description
- `getAppdataVersion(): string` - Version string
- `getAppdataLicense(): string` - License information
- `loadAppdata(cancellable?): Buffer` - Load full appdata XML
- `getInstalledSize(): number` - Installation size in bytes

#### `Ref` (base class for `InstalledRef`, `RemoteRef`, `BundleRef`)

Base class for all reference types.

#### `TransactionOperation`

Individual operation within a transaction.

#### `TransactionProgress`

Progress information for transactions.

#### `Instance`

Running Flatpak instance.

#### `RelatedRef`

Related reference (dependencies).

### Memory Management

Native objects are automatically garbage collected, but you can explicitly free them:

```javascript
const installation = getSystemInstallation();
// ... use installation ...
installation.free(); // Explicit cleanup
```

Or rely on automatic cleanup when objects go out of scope.

## Building from Source

```bash
# Clone the repository
git clone https://github.com/yourusername/libflatpak-js.git
cd libflatpak-js

# Install dependencies
npm install

# Generate bindings and build
npm run build

# Run tests
npm test

# Generate fresh bindings from GIR
python3 generate_from_gir.py
```

### Development Dependencies

- **node-addon-api**: C++ bindings to Node.js
- **node-gyp**: Native module build system
- **pkg-config**: Locates libflatpak headers/libraries
- **Python 3**: For binding generator

## Troubleshooting

### Missing Flatpak Development Libraries

**Fedora/RHEL:**

```bash
sudo dnf install flatpak-devel
```

**Debian/Ubuntu:**

```bash
sudo apt install libflatpak-dev
```

**Arch Linux:**

```bash
sudo pacman -S flatpak
```

### Build Errors

If you encounter build errors:

1. Ensure pkg-config can find libflatpak: `pkg-config --cflags --libs flatpak`
2. Check Node.js version compatibility (requires Node.js 14+)
3. Rebuild native module: `npm rebuild`

### Permission Errors

System installations require appropriate permissions. Consider:

- Using user installations (`~/.local/share/flatpak`)
- Running with appropriate privileges
- Checking Polkit/Flatpak portal permissions

## Roadmap

- [ ] Promise-based async API
- [ ] TypeScript definitions
- [ ] Enhanced error reporting
- [ ] Streaming progress updates
- [ ] Bundle creation support
- [ ] Sandbox inspection tools
- [ ] Portal integration helpers

---

_libflatpak-js is not officially affiliated with the Flatpak project._
