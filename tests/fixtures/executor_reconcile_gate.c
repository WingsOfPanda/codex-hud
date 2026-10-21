#define _GNU_SOURCE
#include <dlfcn.h>
#include <errno.h>
#include <fcntl.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/file.h>
#include <unistd.h>

static void marker(const char *name) {
    char path[4096]; snprintf(path,sizeof(path),"%s/%s",getenv("DIAG_GATE"),name);
    int fd=open(path,O_WRONLY|O_CREAT|O_EXCL,0600); if(fd>=0)close(fd);
}
static int present(const char *name) {
    char path[4096]; snprintf(path,sizeof(path),"%s/%s",getenv("DIAG_GATE"),name);
    return !access(path,F_OK);
}
static void wait_for(const char *name) {
    for(int i=0;i<1500&&!present(name);i++)usleep(10000);
}

/* Keep real flock results; widen the launcher's probe/startup overlap only. */
int flock(int fd,int op) {
    static int (*real)(int,int); static int gated;
    if(!real)real=dlsym(RTLD_NEXT,"flock");
    const char *role=getenv("DIAG_ROLE"), *base=getenv("DIAG_GATE");
    char descriptor[64],path[4096];snprintf(descriptor,sizeof(descriptor),"/proc/self/fd/%d",fd);
    ssize_t n=readlink(descriptor,path,sizeof(path)-1);if(n>=0)path[n]=0;
    int target=role&&base&&n>0&&strstr(path,"/executor.lock")&&(op&LOCK_NB);
    if(target&&!gated&&!strcmp(role,"executor")) {
        marker("executor-ready");
        if(!strcmp(getenv("DIAG_MODE"),"serial")) wait_for("executor-go");
        else for(int i=0;i<1500&&!present("status-ready")&&!present("executor-go");i++)usleep(10000);
    }
    int rc=real(fd,op), saved=errno;
    if(target&&!gated&&!strcmp(role,"launcher")&&rc==0) {
        gated=1;marker("status-ready");wait_for("status-release");
    } else if(target&&!gated&&!strcmp(role,"executor")) {
        gated=1;
        if(rc<0&&(saved==EAGAIN||saved==EWOULDBLOCK)) {
            marker("executor-blocked");wait_for("executor-release");
        }
    }
    if(role&&base&&!strcmp(role,"launcher")&&(op&LOCK_UN)&&n>0&&strstr(path,"/job.lock")&&present("executor-spawned")) {
        if(!present("launch-published")) marker("launch-published");
        else marker("reconcile-completed");
        if(present("status-ready")) marker("launch-job-unlocked");
    }
    errno=saved;return rc;
}

/* Carry this private diagnostic into the executor across production env_clear. */
int execve(const char *path,char *const argv[],char *const envp[]) {
    static int (*real)(const char*,char *const[],char *const[]);
    if(!real)real=dlsym(RTLD_NEXT,"execve");
    const char *role=getenv("DIAG_ROLE");int executor=0;
    for(int i=0;argv[i];i++)if(!strcmp(argv[i],"execution-executor"))executor=1;
    if(role&&!strcmp(role,"launcher")&&executor) {
        marker("executor-spawned");
        int n=0;while(envp[n])n++;
        char **env=calloc(n+5,sizeof(char*));for(int i=0;i<n;i++)env[i]=envp[i];
        if(asprintf(&env[n++],"LD_PRELOAD=%s",getenv("LD_PRELOAD"))<0)return -1;
        if(asprintf(&env[n++],"DIAG_GATE=%s",getenv("DIAG_GATE"))<0)return -1;
        if(asprintf(&env[n++],"DIAG_MODE=%s",getenv("DIAG_MODE"))<0)return -1;
        env[n++]="DIAG_ROLE=executor";env[n]=NULL;
        return real(path,argv,env);
    }
    return real(path,argv,envp);
}
