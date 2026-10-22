/*
 * Arca launcher: resolves its own bundle, points PYTHONHOME at the embedded
 * runtime and runs main.py **inside this very process** via libpython's
 * Py_BytesMain -- no execv.
 *
 * Why no execv: AppKit registers the Services port (Finder's "用 Arca 压缩 /
 * 解压" menu items) only for the process image the bundle declares.  execv-ing
 * into Resources/python/bin/python3.13 changes both the image path and the
 * code-signature identity, and AppKit then never opens the port ("Application
 * com.arca.archive never opened its Services port before the timeout"), so the
 * two services are registered in pbs but unreachable.  dlopen + Py_BytesMain
 * keeps the process as Contents/MacOS/Arca end to end.
 */
#include <dlfcn.h>
#include <limits.h>
#include <mach-o/dyld.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <unistd.h>

static void drop_last_component(char *path) {
    char *slash = strrchr(path, '/');
    if (slash) *slash = '\0';
}

int main(int argc, char **argv) {
    char exe[PATH_MAX];
    char resolved[PATH_MAX];
    uint32_t size = sizeof(exe);
    if (_NSGetExecutablePath(exe, &size) != 0) {
        fprintf(stderr, "Arca: executable path too long\n");
        return 1;
    }
    if (!realpath(exe, resolved)) {
        strncpy(resolved, exe, sizeof(resolved) - 1);
        resolved[sizeof(resolved) - 1] = '\0';
    }

    /* .../Arca.app/Contents/MacOS/Arca -> .../Arca.app/Contents */
    drop_last_component(resolved);   /* MacOS  */
    drop_last_component(resolved);   /* Contents */

    char python_home[PATH_MAX];
    char libpython[PATH_MAX];
    char script[PATH_MAX];
    snprintf(python_home, sizeof(python_home), "%s/Resources/python", resolved);
    snprintf(libpython, sizeof(libpython), "%s/lib/libpython3.13.dylib", python_home);
    snprintf(script, sizeof(script), "%s/Resources/app/main.py", resolved);

    if (access(libpython, R_OK) != 0) {
        fprintf(stderr, "Arca: embedded libpython missing at %s\n", libpython);
        return 1;
    }

    setenv("PYTHONHOME", python_home, 1);
    setenv("PYTHONDONTWRITEBYTECODE", "1", 1);
    setenv("PYTHONNOUSERSITE", "1", 1);
    /* keep the bundle self-contained even if the user has PYTHONPATH set */
    unsetenv("PYTHONPATH");
    unsetenv("PYTHONSTARTUP");

    void *py = dlopen(libpython, RTLD_LAZY | RTLD_GLOBAL);
    if (!py) {
        fprintf(stderr, "Arca: could not load %s: %s\n", libpython, dlerror());
        return 1;
    }
    int (*py_main)(int, char **) = (int (*)(int, char **))dlsym(py, "Py_BytesMain");
    if (!py_main) {
        fprintf(stderr, "Arca: Py_BytesMain missing: %s\n", dlerror());
        return 1;
    }

    /* python argv: [argv0, main.py, <user args...>] */
    char **args = calloc((size_t)argc + 2, sizeof(char *));
    if (!args) return 1;
    args[0] = (char *)"python";
    args[1] = script;
    for (int i = 1; i < argc; i++) args[i + 1] = argv[i];
    args[argc + 1] = NULL;

    return py_main(argc + 1, args);
}
