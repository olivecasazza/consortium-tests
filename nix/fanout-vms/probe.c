/* Readiness probe: stdin -> FILE -> stdout, with an fsync in between.
 *
 * The harness needs a VM to prove it can move data and make it durable, so it
 * sends a fresh random nonce on stdin and requires those exact bytes back. The
 * shell pipeline this replaces -- `umask 077 && cat > f && sync f && cat f` --
 * costs three execs (cat, sync, cat) on top of the login shell sshd always
 * spawns. All 64 probes run at once and the probe tail is guest-vCPU bound, so
 * those execs are pure overhead: one binary is sh plus this, not sh plus four.
 *
 * Nothing here trusts the file. The bytes printed come from a second open() of
 * the path after fsync and close, so a passing probe means the data survived a
 * write, a flush to the block device, and a re-read -- which is the whole
 * point, and is why the file must live on the block-backed volume rather than
 * the initrd tmpfs, where fsync is a no-op that would prove nothing.
 */

#define _GNU_SOURCE

#include <errno.h>
#include <fcntl.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <unistd.h>
/* Write every byte or report why not. A short write is normal, not an error. */
static int write_all(int fd, const char *buf, size_t len)
{
    size_t off = 0;

    while (off < len) {
        ssize_t written = write(fd, buf + off, len - off);
        if (written < 0) {
            if (errno == EINTR)
                continue;
            fprintf(stderr, "write: %s\n", strerror(errno));
            return -1;
        }
        off += (size_t)written;
    }
    return 0;
}

/* Second mode: emit N bytes of fresh kernel entropy on stdout.
 *
 * Every restore shares the captured RAM image, so the harness has to be able
 * to tell whether two nodes are drawing from independent RNG state or
 * replaying one captured stream. It lives here rather than in a shell
 * pipeline so the check does not depend on which coreutils the guest's PATH
 * happens to contain.
 */
static int emit_entropy(long want)
{
    int fd = open("/dev/urandom", O_RDONLY);

    if (fd < 0) {
        fprintf(stderr, "open /dev/urandom: %s\n", strerror(errno));
        return -1;
    }
    char buf[512];
    long left = want;

    while (left > 0) {
        size_t ask = (size_t)(left < (long)sizeof(buf) ? left : (long)sizeof(buf));
        ssize_t got = read(fd, buf, ask);

        if (got < 0) {
            if (errno == EINTR)
                continue;
            fprintf(stderr, "read /dev/urandom: %s\n", strerror(errno));
            close(fd);
            return -1;
        }
        if (got == 0) {
            fprintf(stderr, "/dev/urandom returned short\n");
            close(fd);
            return -1;
        }
        if (write_all(STDOUT_FILENO, buf, (size_t)got) != 0) {
            close(fd);
            return -1;
        }
        left -= got;
    }
    close(fd);
    return 0;
}

static int read_all(int fd, char **buf, size_t *len, size_t *cap)
{
    for (;;) {
        if (*len == *cap) {
            size_t grown = *cap ? *cap * 2 : 4096;
            char *next = realloc(*buf, grown);
            if (next == NULL) {
                fprintf(stderr, "out of memory\n");
                return -1;
            }
            *buf = next;
            *cap = grown;
        }
        ssize_t n = read(fd, *buf + *len, *cap - *len);
        if (n < 0) {
            if (errno == EINTR)
                continue;
            fprintf(stderr, "read: %s\n", strerror(errno));
            return -1;
        }
        if (n == 0)
            return 0;
        *len += (size_t)n;
    }
}

int main(int argc, char **argv)
{
    char *buf = NULL;
    size_t len = 0, cap = 0;
    int rc = 1;

    if (argc == 3 && strcmp(argv[1], "--entropy") == 0) {
        char *end = NULL;
        long want = strtol(argv[2], &end, 10);

        if (!end || *end != '\0' || want <= 0 || want > 65536) {
            fprintf(stderr, "usage: %s --entropy BYTES (1..65536)\n", argv[0]);
            return 2;
        }
        return emit_entropy(want) == 0 ? 0 : 1;
    }
    if (argc != 2 || argv[1][0] == '-') {
        /* A path starting with '-' is a mistyped flag, not a file. Without
         * this, `--entropy` with no count falls through to the file branch,
         * creates a file by that name, and then blocks reading stdin. */
        fprintf(stderr, "usage: %s FILE\n"
                        "       %s --entropy BYTES\n", argv[0], argv[0]);
        return 2;
    }
    const char *path = argv[1];

    if (read_all(STDIN_FILENO, &buf, &len, &cap) != 0)
        goto out;

    /* 0600 directly, rather than leaning on a umask the caller must remember
     * to set before spawning us. */
    int fd = open(path, O_WRONLY | O_CREAT | O_TRUNC, 0600);
    if (fd < 0) {
        fprintf(stderr, "open %s for writing: %s\n", path, strerror(errno));
        goto out;
    }
    if (write_all(fd, buf, len) != 0) {
        fprintf(stderr, "write %s: %s\n", path, strerror(errno));
        close(fd);
        goto out;
    }
    if (fsync(fd) != 0) {
        fprintf(stderr, "fsync %s: %s\n", path, strerror(errno));
        close(fd);
        goto out;
    }
    if (close(fd) != 0) {
        fprintf(stderr, "close %s: %s\n", path, strerror(errno));
        goto out;
    }

    int in = open(path, O_RDONLY);
    if (in < 0) {
        fprintf(stderr, "reopen %s: %s\n", path, strerror(errno));
        goto out;
    }
    size_t echoed = 0;
    for (;;) {
        ssize_t n = read(in, buf, cap);
        if (n < 0) {
            if (errno == EINTR)
                continue;
            fprintf(stderr, "read back %s: %s\n", path, strerror(errno));
            close(in);
            goto out;
        }
        if (n == 0)
            break;
        if (write_all(STDOUT_FILENO, buf, (size_t)n) != 0) {
            close(in);
            goto out;
        }
        echoed += (size_t)n;
    }
    close(in);

    /* A short read-back means the file did not hold what was written, which
     * would let the harness compare fewer bytes than it sent. */
    if (echoed != len) {
        fprintf(stderr, "%s: wrote %zu bytes, read back %zu\n", path, len, echoed);
        goto out;
    }
    rc = 0;

out:
    free(buf);
    return rc;
}
