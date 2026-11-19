/* Neškodná fixtura pro akceptační test RE (fáze D, rozšíření D3–D6). */
#include <stdio.h>
#include <string.h>

static unsigned checksum(const char *s) {
    unsigned h = 2166136261u;           /* FNV-1a */
    for (; *s; s++) { h ^= (unsigned char)*s; h *= 16777619u; }
    return h;
}

int main(int argc, char **argv) {
    const char *in = argc > 1 ? argv[1] : "agent-lease";
    if (strlen(in) > 64) { fputs("input too long\n", stderr); return 2; }
    printf("%08x\n", checksum(in));
    return 0;
}
