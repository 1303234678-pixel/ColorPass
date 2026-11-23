// ColorPass macOS Reader —— 常驻解密服务（dylib，注入剪映进程）
//
// 设计目标：
//   1. 注入后在剪映进程内常驻，暴露一个 Unix socket 服务；
//   2. 收到请求才解密，剪映正常使用不受影响（阻塞在 accept，零 CPU）；
//   3. 密钥始终不出进程，只交出解密后的明文；
//   4. 只读：DECRYPT 只读输入文件，不写剪映任何文件、不改草稿、不改设置。
//
// 为什么没有 ENCRYPT（重要，别加回来）：
//   2026-11-23 在 11.8 上实测：进程内调用 lvve::EncryptUtils::encrypt 会让剪映
//   **直接崩溃**（进程消失），此时 isEnable() 返回 0 —— 说明 11.7+ 的加密通道
//   本身是关闭的，符号还在但不可用。所以写回不能走「自己加密再落盘」这条路，
//   只能生成明文草稿让剪映自己读入并重新加密。
//
// 协议（文本行 + 原始字节）：
//   请求：DECRYPT <utf8-path>\n
//   成功：OK <byte-length>\n<raw-json-bytes>
//   失败：ERR <message>\n
//   状态：PING\n -> PONG <pid>\n

#include <cerrno>
#include <csignal>
#include <cstdarg>
#include <cstdio>
#include <cstdlib>
#include <cstring>
#include <ctime>
#include <dlfcn.h>
#include <fcntl.h>
#include <pthread.h>
#include <pwd.h>
#include <string>
#include <sys/socket.h>
#include <sys/stat.h>
#include <sys/time.h>
#include <sys/un.h>
#include <unistd.h>

static const char *kFw =
    "/Applications/VideoFusion-macOS.app/Contents/Frameworks/libvideoeditor.dylib";

// lvve::EncryptUtils::decrypt(string const&, string const&, bool&)
// （nm -gU 取出后去掉 Mach-O 前导下划线）
static const char *kSymDecrypt =
    "_ZN4lvve12EncryptUtils7decryptERKNSt3__112basic_stringIcNS1_11char_traitsIcEENS1_9allocatorIcEEEES9_Rb";

static char g_dir[512];
static char g_sock_path[600];
static char g_log_path[600];

static void build_paths() {
  const char *home = getenv("HOME");
  if (!home) {
    struct passwd *pw = getpwuid(getuid());
    home = pw ? pw->pw_dir : "/tmp";
  }
  snprintf(g_dir, sizeof(g_dir), "%s/Library/Application Support/ColorPass", home);
  snprintf(g_sock_path, sizeof(g_sock_path), "%s/reader.sock", g_dir);
  snprintf(g_log_path, sizeof(g_log_path), "%s/reader.log", g_dir);
}

static void lg(const char *fmt, ...) {
  if (!g_log_path[0]) build_paths();
  int fd = open(g_log_path, O_WRONLY | O_CREAT | O_APPEND, 0600);
  if (fd < 0) return;
  char buf[4096];
  int n = snprintf(buf, sizeof(buf), "[%ld pid=%d] ", (long)time(nullptr), (int)getpid());
  va_list ap;
  va_start(ap, fmt);
  n += vsnprintf(buf + n, sizeof(buf) - n, fmt, ap);
  va_end(ap);
  if (n > (int)sizeof(buf) - 2) n = (int)sizeof(buf) - 2;
  buf[n++] = '\n';
  ssize_t ignored = write(fd, buf, n);
  (void)ignored;
  close(fd);
}

static bool slurp(const char *p, std::string &out) {
  FILE *f = fopen(p, "rb");
  if (!f) return false;
  char b[65536];
  size_t r;
  while ((r = fread(b, 1, sizeof(b), f)) > 0) out.append(b, r);
  fclose(f);
  return true;
}

typedef std::string (*decrypt_t)(void *, const std::string &, const std::string &, bool &);

static decrypt_t g_decrypt = nullptr;
static pthread_mutex_t g_resolve_mu = PTHREAD_MUTEX_INITIALIZER;

// 缓存解析到的函数指针；剪映自己也会加载该库，优先复用已加载实例。
static decrypt_t resolve_decrypt(int *waited_ms) {
  pthread_mutex_lock(&g_resolve_mu);
  if (g_decrypt) {
    pthread_mutex_unlock(&g_resolve_mu);
    if (waited_ms) *waited_ms = 0;
    return g_decrypt;
  }
  // 最多等 40 秒，等剪映自己把该 dylib 加载起来，避免重复初始化。
  int waited = 0;
  void *h = nullptr;
  for (int i = 0; i < 80; i++) {
    h = dlopen(kFw, RTLD_LAZY | RTLD_NOLOAD);
    if (h) break;
    usleep(500 * 1000);
    waited += 500;
  }
  if (!h) h = dlopen(kFw, RTLD_LAZY | RTLD_GLOBAL);  // 兜底：自己加载
  if (!h) {
    lg("resolve: dlopen failed: %s", dlerror());
    pthread_mutex_unlock(&g_resolve_mu);
    if (waited_ms) *waited_ms = waited;
    return nullptr;
  }
  g_decrypt = (decrypt_t)dlsym(h, kSymDecrypt);
  lg("resolve: %dms decrypt=%p", waited, (void *)g_decrypt);
  pthread_mutex_unlock(&g_resolve_mu);
  if (waited_ms) *waited_ms = waited;
  return g_decrypt;
}

static bool send_all(int fd, const char *p, size_t n) {
  size_t off = 0;
  while (off < n) {
    ssize_t w = write(fd, p + off, n - off);
    if (w <= 0) return false;
    off += (size_t)w;
  }
  return true;
}

static void send_err(int fd, const char *msg) {
  char e[600];
  int n = snprintf(e, sizeof(e), "ERR %s\n", msg);
  send_all(fd, e, (size_t)n);
}

static void do_decrypt(int cfd, const char *path) {
  struct timeval t0, t1;
  gettimeofday(&t0, nullptr);

  std::string cipher;
  if (!slurp(path, cipher) || cipher.empty()) {
    send_err(cfd, "cannot-read-input");
    lg("decrypt: input unreadable path=%s", path);
    return;
  }
  int waited = 0;
  decrypt_t fn = resolve_decrypt(&waited);
  if (!fn) {
    send_err(cfd, "crypto-unavailable");
    return;
  }
  char self[128];
  memset(self, 0, sizeof(self));
  bool ok = false;
  std::string plain = fn(self, cipher, std::string(), ok);

  gettimeofday(&t1, nullptr);
  long ms = (t1.tv_sec - t0.tv_sec) * 1000 + (t1.tv_usec - t0.tv_usec) / 1000;

  if (!ok || plain.empty()) {
    send_err(cfd, "empty-result");
    lg("decrypt: empty ok=%d in=%zu waited=%dms", (int)ok, cipher.size(), waited);
    return;
  }
  char hdr[128];
  int hn = snprintf(hdr, sizeof(hdr), "OK %zu\n", plain.size());
  send_all(cfd, hdr, (size_t)hn);
  send_all(cfd, plain.data(), plain.size());
  lg("decrypt: ok in=%zu out=%zu %ldms head=%.24s", cipher.size(), plain.size(), ms,
     plain.c_str());
}

static void handle_conn(int cfd) {
  char line[2048];
  size_t used = 0;
  while (used < sizeof(line) - 1) {
    char c;
    ssize_t r = read(cfd, &c, 1);
    if (r <= 0) return;
    if (c == '\n') break;
    line[used++] = c;
  }
  line[used] = '\0';

  if (strncmp(line, "PING", 4) == 0) {
    char out[64];
    int n = snprintf(out, sizeof(out), "PONG %d\n", (int)getpid());
    send_all(cfd, out, (size_t)n);
    return;
  }
  if (strncmp(line, "DECRYPT ", 8) == 0) {
    do_decrypt(cfd, line + 8);
    return;
  }
  send_err(cfd, "bad-request");
}

static void *server(void *) {
  struct stat st;
  if (stat(g_dir, &st) != 0) {
    std::string p(g_dir);
    for (size_t i = 1; i < p.size(); i++) {
      if (p[i] == '/') {
        std::string sub = p.substr(0, i);
        mkdir(sub.c_str(), 0700);
      }
    }
    mkdir(g_dir, 0700);
    chmod(g_dir, 0700);
  }

  unlink(g_sock_path);
  int sfd = socket(AF_UNIX, SOCK_STREAM, 0);
  if (sfd < 0) {
    lg("server: socket() failed errno=%d", errno);
    return nullptr;
  }
  struct sockaddr_un addr;
  memset(&addr, 0, sizeof(addr));
  addr.sun_family = AF_UNIX;
  snprintf(addr.sun_path, sizeof(addr.sun_path), "%s", g_sock_path);
  if (bind(sfd, (struct sockaddr *)&addr, sizeof(addr)) != 0) {
    lg("server: bind failed errno=%d path=%s", errno, g_sock_path);
    close(sfd);
    return nullptr;
  }
  chmod(g_sock_path, 0600);
  if (listen(sfd, 8) != 0) {
    lg("server: listen failed errno=%d", errno);
    close(sfd);
    return nullptr;
  }
  lg("server: READY listening on %s", g_sock_path);

  for (;;) {
    int cfd = accept(sfd, nullptr, nullptr);
    if (cfd < 0) {
      if (errno == EINTR) continue;
      lg("server: accept failed errno=%d", errno);
      usleep(200 * 1000);
      continue;
    }
    handle_conn(cfd);
    close(cfd);
  }
  return nullptr;
}

__attribute__((constructor)) static void cpr_boot(void) {
  // 必须忽略 SIGPIPE：本 dylib 跑在剪映进程里。客户端（面板/CLI）在解密结果
  // 写回之前断开 socket（关窗口、超时、进程被杀）时，send_all 的 write 会触发
  // SIGPIPE，默认动作是终止进程 —— 也就是**剪映整个被崩掉**。忽略它之后 write
  // 只返回 EPIPE，send_all 返回 false，读连接正常关闭，剪映毫发无损。
  signal(SIGPIPE, SIG_IGN);

  build_paths();
  lg("=== ColorPass Reader LOADED ===");
  pthread_t t;
  if (pthread_create(&t, nullptr, server, nullptr) == 0) {
    pthread_detach(t);
  } else {
    lg("boot: pthread_create failed");
  }
}
