/* Trusted temporary smoke test. No Windows SDK or runtime required. */
#define WIN __attribute__((ms_abi))
typedef unsigned int u32;
typedef unsigned long long u64;
typedef int hr;
typedef void *ptr;
extern ptr (WIN *pLoadLibraryA)(const char *);
extern ptr (WIN *pGetProcAddress)(ptr, const char *);
extern ptr (WIN *pGetStdHandle)(u32);
extern ptr (WIN *pCreateFileA)(const char*,u32,u32,ptr,u32,u32,ptr);
extern int (WIN *pWriteFile)(ptr, const void *, u32, u32 *, ptr);
extern void (WIN *pExitProcess)(u32);
struct guid {u32 a; unsigned short b, c; unsigned char d[8];};
static const struct guid iid_dxgi_device = {0x54ec77fa,0x1377,0x44e6,{0x8c,0x32,0x88,0xfd,0x5f,0x44,0xc8,0x4c}};
static const struct guid iid_d3d12_device = {0x189819f1,0x1db6,0x4b57,{0xbe,0x54,0x18,0x21,0x33,0x9b,0x85,0xf7}};
struct adapter_desc {unsigned short name[128];u32 vendor,device,subsys,revision;u64 video,system,shared,luid;};
static ptr output_file;
static void write_text(const char *text) {
    u32 n=0,written=0;
    while(text[n]) ++n;
    pWriteFile(output_file,text,n,&written,0);
}
static void write_hex(const char *label,u32 value) {
    char out[11]={'0','x',0,0,0,0,0,0,0,0,0};
    const char *digits="0123456789abcdef";
    int i;
    for(i=0;i<8;++i) out[9-i]=digits[(value>>(i*4))&15];
    write_text(label);write_text(out);write_text("\r\n");
}
static void release(ptr obj) {if(obj) ((u32(WIN*)(ptr))(*(ptr**)obj)[2])(obj);}
void WIN entry(void) {
    output_file=pCreateFileA("graphics-result.txt",0x40000000,1,0,2,0x80,0);
    write_text("TRUSTED_GRAPHICS_PROBE_ENTRY\r\n");
    ptr dev=0,ctx=0,dxgi=0,adapter=0,dev12=0;
    u32 level=0;
    ptr lib11=pLoadLibraryA("d3d11.dll"),lib12=pLoadLibraryA("d3d12.dll");
    hr (WIN *create11)(ptr,u32,ptr,u32,ptr,u32,u32,ptr*,u32*,ptr*)=(ptr)pGetProcAddress(lib11,"D3D11CreateDevice");
    hr (WIN *create12)(ptr,u32,const struct guid*,ptr*)=(ptr)pGetProcAddress(lib12,"D3D12CreateDevice");
    hr result11=create11 ? create11(0,1,0,0,0,0,7,&dev,&level,&ctx) : (int)0x8007007e;
    write_hex("D3D11CreateDevice=",(u32)result11);
    write_hex("D3D11FeatureLevel=",level);
    u32 vendor=0;
    if(result11>=0) {
        hr q=((hr(WIN*)(ptr,const struct guid*,ptr*))(*(ptr**)dev)[0])(dev,&iid_dxgi_device,&dxgi);
        if(q>=0) {
            hr a=((hr(WIN*)(ptr,ptr*))(*(ptr**)dxgi)[7])(dxgi,&adapter);
            if(a>=0) {
                struct adapter_desc desc;
                hr d=((hr(WIN*)(ptr,struct adapter_desc*))(*(ptr**)adapter)[8])(adapter,&desc);
                if(d>=0) {
                    vendor=desc.vendor;
                    write_hex("DXGIAdapterVendorID=",vendor);
                    write_hex("DXGIAdapterDeviceID=",desc.device);
                    write_text("DXGIAdapterDescription=");
                    char name[129];int i;
                    for(i=0;i<128 && desc.name[i];++i) name[i]=desc.name[i]<128 ? desc.name[i] : '?';
                    name[i]=0;write_text(name);write_text("\r\n");
                }
            }
        }
    }
    release(dxgi);dxgi=0;release(ctx);ctx=0;release(dev);dev=0;
    hr result12=create12 ? create12(adapter,0xb000,&iid_d3d12_device,&dev12) : (int)0x8007007e;
    write_hex("D3D12CreateDevice=",(u32)result12);
    release(dev12);dev12=0;
    hr result12default=create12 ? create12(0,0xb000,&iid_d3d12_device,&dev12) : (int)0x8007007e;
    write_hex("D3D12DefaultAdapter=",(u32)result12default);
    release(dev12);release(adapter);release(dxgi);release(ctx);release(dev);
    pExitProcess(result11<0 || result12<0 || vendor==0 ? 1 : 0);
}
