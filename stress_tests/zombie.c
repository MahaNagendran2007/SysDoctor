/* Classic zombie demo in C.  gcc zombie.c -o zombie && ./zombie 5 */
#include <stdio.h>
#include <stdlib.h>
#include <unistd.h>

int main(int argc, char *argv[]) {
    int n = argc > 1 ? atoi(argv[1]) : 5;
    printf("Parent PID %d\n", getpid());
    for (int i = 0; i < n; i++) {
        pid_t pid = fork();
        if (pid == 0) exit(0);          /* child terminates immediately */
        printf("child %d exited -> zombie\n", pid);
    }
    for (;;) sleep(1);                  /* parent never calls wait() */
    return 0;
}
