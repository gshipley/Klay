.section .idata$2,"aw"
.balign 8
.long ilt - 0x400000
.long 0
.long 0
.long dllname - 0x400000
.long iat - 0x400000
.section .idata$4,"aw"
.balign 8
ilt:
.quad hint_load - 0x400000
.quad hint_proc - 0x400000
.quad hint_stdout - 0x400000
.quad hint_create - 0x400000
.quad hint_write - 0x400000
.quad hint_exit - 0x400000
.quad 0
.section .idata$5,"aw"
iat:
.global pLoadLibraryA
pLoadLibraryA: .quad hint_load - 0x400000
.global pGetProcAddress
pGetProcAddress: .quad hint_proc - 0x400000
.global pGetStdHandle
pGetStdHandle: .quad hint_stdout - 0x400000
.global pCreateFileA
pCreateFileA: .quad hint_create - 0x400000
.global pWriteFile
pWriteFile: .quad hint_write - 0x400000
.global pExitProcess
pExitProcess: .quad hint_exit - 0x400000
.quad 0
.section .idata$6,"aw"
dllname: .asciz "KERNEL32.dll"
.balign 2
hint_load: .short 0
.asciz "LoadLibraryA"
.balign 2
hint_proc: .short 0
.asciz "GetProcAddress"
.balign 2
hint_stdout: .short 0
.asciz "GetStdHandle"
.balign 2
hint_write: .short 0
.asciz "WriteFile"
.balign 2
hint_create: .short 0
.asciz "CreateFileA"
.balign 2
hint_exit: .short 0
.asciz "ExitProcess"
