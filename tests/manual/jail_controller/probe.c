/* Trusted connection/capability probe; never reads buttons or sets vibration. */
#define WIN __attribute__((ms_abi))
typedef unsigned int u32;
typedef unsigned short u16;
typedef unsigned char u8;
typedef void *ptr;
extern ptr (WIN *pLoadLibraryA)(const char *);
extern ptr (WIN *pGetProcAddress)(ptr, const char *);
extern ptr (WIN *pCreateFileA)(const char *,u32,u32,ptr,u32,u32,ptr);
extern int (WIN *pWriteFile)(ptr,const void *,u32,u32 *,ptr);
extern void (WIN *pExitProcess)(u32);
struct capabilities {u8 type,subtype;u16 flags;u8 gamepad[12];u16 motors[2];};
static ptr output;
static void text(const char *value) {
    u32 size=0,written=0;
    while(value[size]) ++size;
    pWriteFile(output,value,size,&written,0);
}
static void hex(const char *label,u32 value) {
    char out[11]={'0','x',0,0,0,0,0,0,0,0,0};
    const char *digits="0123456789abcdef";
    int i;
    for(i=0;i<8;++i) out[9-i]=digits[(value>>(i*4))&15];
    text(label);text(out);text("\r\n");
}
void WIN entry(void) {
    ptr library=pLoadLibraryA("xinput1_4.dll");
    u32 (WIN *get_capabilities)(u32,u32,struct capabilities *)=(ptr)pGetProcAddress(library,"XInputGetCapabilities");
    ptr kernel=pLoadLibraryA("kernel32.dll");
    void (WIN *sleep)(u32)=(ptr)pGetProcAddress(kernel,"Sleep");
    output=pCreateFileA("controller-result.txt",0x40000000,1,0,2,0x80,0);
    text("TRUSTED_CONTROLLER_CAPABILITIES_PROBE\r\n");
    if(!get_capabilities || !sleep) {text("XInputLibraryUnavailable\r\n");pExitProcess(3);}
    u32 count=0,index,attempt;
    /* Winebus can finish enumerating after the process begins. */
    for(attempt=0;attempt<20;++attempt) {
        count=0;
        for(index=0;index<4;++index) {
            struct capabilities caps;
            if(get_capabilities(index,0,&caps)==0) ++count;
        }
        if(count) break;
        sleep(500);
    }
    hex("XInputConnectedCount=",count);
    for(index=0;index<4;++index) {
        struct capabilities caps;
        u32 result=get_capabilities(index,0,&caps);
        hex("Slot=",index);hex("Status=",result);
        if(result==0) {
            hex("Type=",caps.type);hex("SubType=",caps.subtype);hex("Flags=",caps.flags);
        }
    }
    pExitProcess(count ? 0 : 2);
}
