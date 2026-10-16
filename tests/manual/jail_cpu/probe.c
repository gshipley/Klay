/* Trusted CPU topology smoke test; no Windows SDK or C runtime required. */
#define WIN __attribute__((ms_abi))
typedef unsigned int u32;
typedef unsigned long long u64;
typedef void *ptr;
extern ptr (WIN *pLoadLibraryA)(const char *);
extern ptr (WIN *pGetProcAddress)(ptr, const char *);
extern ptr (WIN *pCreateFileA)(const char *, u32, u32, ptr, u32, u32, ptr);
extern int (WIN *pWriteFile)(ptr, const void *, u32, u32 *, ptr);
extern void (WIN *pExitProcess)(u32);
struct system_info {
    u32 architecture, page_size;
    ptr minimum_address, maximum_address;
    u64 active_mask;
    u32 processors, type, allocation_granularity;
    unsigned short level, revision;
};
struct logical_info {u64 mask; u32 relationship, padding; u64 data[2];};
static struct logical_info logical[4096];
static ptr output;
static void text(const char *value) {
    u32 length = 0, written = 0;
    while (value[length]) ++length;
    pWriteFile(output, value, length, &written, 0);
}
static void number(const char *label, u64 value) {
    char buffer[32];
    int i = 31;
    buffer[i] = 0;
    do {buffer[--i] = '0' + value % 10; value /= 10;} while (value);
    text(label); text(buffer + i); text("\r\n");
}
void WIN entry(void) {
    output = pCreateFileA("cpu-result.txt", 0x40000000, 1, 0, 2, 0x80, 0);
    ptr kernel = pLoadLibraryA("kernel32.dll");
    void (WIN *system)(struct system_info *) = (ptr)pGetProcAddress(kernel, "GetSystemInfo");
    u32 (WIN *active)(unsigned short) = (ptr)pGetProcAddress(kernel, "GetActiveProcessorCount");
    int (WIN *info)(struct logical_info *, u32 *) = (ptr)pGetProcAddress(kernel, "GetLogicalProcessorInformation");
    int (WIN *affinity)(ptr, u64 *, u64 *) = (ptr)pGetProcAddress(kernel, "GetProcessAffinityMask");
    u32 (WIN *last_error)(void) = (ptr)pGetProcAddress(kernel, "GetLastError");
    struct system_info system_info;
    if (!system || !active || !info || !affinity || !last_error) pExitProcess(2);
    system(&system_info);
    number("GetSystemInfo.count=", system_info.processors);
    number("GetSystemInfo.mask=", system_info.active_mask);
    u32 active_count = active(0xffff);
    number("GetActiveProcessorCount=", active_count);
    u64 process_mask = 0, system_mask = 0;
    number("GetProcessAffinityMask.ok=", affinity((ptr)-1, &process_mask, &system_mask));
    number("ProcessMask=", process_mask);
    number("SystemMask=", system_mask);
    u32 length = sizeof(logical);
    int ok = info(logical, &length);
    u32 error = ok ? 0 : last_error();
    number("GetLogicalProcessorInformation.ok=", ok);
    number("GetLogicalProcessorInformation.error=", error);
    number("GetLogicalProcessorInformation.bytes=", length);
    u32 cores = 0, caches = 0, nodes = 0, packages = 0;
    u64 mask = 0;
    if (ok) {
        for (u32 i = 0; i < length / sizeof(logical[0]); ++i) {
            switch (logical[i].relationship) {
                case 0: ++cores; mask |= logical[i].mask; break;
                case 1: ++nodes; break;
                case 2: ++caches; break;
                case 3: ++packages; break;
            }
        }
    }
    number("Cores=", cores);
    number("Caches=", caches);
    number("NumaNodes=", nodes);
    number("Packages=", packages);
    number("LogicalMask=", mask);
    pExitProcess(!ok || !active_count || !cores || !nodes || !packages ? 1 : 0);
}
