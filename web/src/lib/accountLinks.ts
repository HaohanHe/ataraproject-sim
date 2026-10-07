/**
 * Where a signed-in contestant finds their own things: the header account menu and the
 * dashboard's quick links (owner feedback: the 控制台 link was too easy to miss, and few
 * knew it holds the personal API tokens). Hash links use the anchors in lib/deepLink.
 */
export interface AccountLink { to: string; en: string; zh: string; testid: string; hintEn?: string; hintZh?: string }

export const accountMenuItems: AccountLink[] = [
  { to: '/dashboard', en: 'Dashboard', zh: '控制台', testid: 'account-dashboard' },
  { to: '/team', en: 'Team', zh: '队伍', testid: 'account-team' },
  { to: '/profile', en: 'Profile', zh: '个人资料', testid: 'account-profile' },
  { to: '/profile#api-tokens', en: 'Personal API tokens (CLI / Kimi relay)', zh: '个人 API 令牌（命令行 / Kimi 中转）', testid: 'account-api-tokens' },
]

export const dashboardQuickLinks: AccountLink[] = [
  { to: '/profile#api-tokens', en: 'Personal API tokens', zh: '个人 API 令牌', testid: 'quick-api-tokens',
    hintEn: 'For the command-line tool and the temporary Kimi relay', hintZh: '命令行工具、Kimi 临时中转都用它' },
  { to: '/profile#kimi-relay', en: 'Temporary Kimi relay', zh: '平台临时 Kimi 中转', testid: 'quick-kimi-relay',
    hintEn: 'Base URL and your team\'s allowance today', hintZh: '接口地址与本队今日剩余额度' },
  { to: '/cli', en: 'Command-line tool guide', zh: '命令行工具说明', testid: 'quick-cli',
    hintEn: 'Install survey26 and sign in with a token', hintZh: '安装 survey26，用令牌登录' },
  { to: '/team#requests', en: 'Team & join requests', zh: '队伍与入队申请', testid: 'quick-team',
    hintEn: 'Members, invite code, requests waiting for you', hintZh: '成员、邀请码、待处理申请' },
  { to: '/profile#wechat-qr', en: 'WeChat QR code', zh: '微信二维码', testid: 'quick-wechat-qr',
    hintEn: 'Upload yours so teammates can add you', hintZh: '上传后队友可以加你' },
]
