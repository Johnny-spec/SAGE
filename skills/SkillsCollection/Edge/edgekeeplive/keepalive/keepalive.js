/**
 * 柜员超时监听配置
 */
let currentTime = new Date().getTime()
const TELLER_TIMEOUT_CONFIG = {
  MOVE_SEC: 25 * 60, // 25 分钟未操作阈值（秒）
  SIGNOUT_COUNTDOWN_MIN: 3, // 签退倒计时（分钟）
  SESSION_PROBE_MS: 14 * 60 * 1000, // 会话探活触发时间（毫秒）
  CTIE_SESSION_PROBE_MIN: (() => {
    const val = window.applicationVar?.nctbSessionProbeMin
    const num = Number(val)
    return Number.isFinite(num) && num > 0 ? num : 5
  })() // ctie 页面会话探活间隔（分钟），默认 5 分钟
}

/**
 * 公共方法
 */
class CommonService {
  static isNumber(str) {
    const num = Number(str)
    return !!str && Number.isNaN(num) === false && typeof num === 'number'
  }

  /**
   * 会话探活
   * @param {number} nowTimestamp - 当前时间戳（毫秒）
   * @param {boolean} isCtiePage - 是否为 ctie 页面
   */
  static probeSession(nowTimestamp, isCtiePage = false) {
    const elapsed = nowTimestamp - currentTime
    const probeThreshold = isCtiePage
      ? TELLER_TIMEOUT_CONFIG.CTIE_SESSION_PROBE_MIN * 60 * 1000
      : TELLER_TIMEOUT_CONFIG.SESSION_PROBE_MS

    if (elapsed > probeThreshold) {
      currentTime = nowTimestamp
      CommonRepo.sessionValid()
    }
  }

  /**
   * 监听柜员长时间未操作，自动签退
   * @param {Object} store - Vuex store 实例
   * @returns {Promise<boolean>} true-超时自动签退，false-手动取消签退
   */
  static async listenerTellerWorking(store) {
    let moveLastTime = Date.now()
    let validFlag = true

    // 日志记录
    const log = msg => {
      Vue.prototype.$logger.info('click', { msg: `[cssp-listener-teller-working]${msg}` })
    }

    // 获取柜员登录状态
    const isLogin = () => {
      const { zoneNo, brNo, tellerNo } = store.getters.globalVar
      return zoneNo && brNo && tellerNo
    }

    // 检查是否超时
    const checkIsTimeout = () => {
      const nowTimestamp = Date.now()
      const elapsedMs = nowTimestamp - moveLastTime
      window.isDev &&
        console.warn(elapsedMs, '>', TELLER_TIMEOUT_CONFIG.SESSION_PROBE_MS, validFlag)

      // 中间探活：避免等待时长太久导致后端会话过期
      if (elapsedMs > TELLER_TIMEOUT_CONFIG.SESSION_PROBE_MS && validFlag) {
        validFlag = false
        CommonRepo.sessionValid()
      }

      return elapsedMs > TELLER_TIMEOUT_CONFIG.MOVE_SEC * 1000
    }

    // 更新最后操作时间
    const setLastTime = (isCtiePage = false) => {
      moveLastTime = Date.now()
      validFlag = true // 页面操作时重置
      CommonService.probeSession(moveLastTime, isCtiePage === true)
    }

    // 鼠标操作监听
    const listenerMouse = () => {
      window.addEventListener('click', setLastTime)
      window.addEventListener('keydown', setLastTime)
    }

    const removeListenerMouse = () => {
      window.removeEventListener('click', setLastTime)
      window.removeEventListener('keydown', setLastTime)
    }

    // 签退提示对话框
    const showTimeoutConfirm = async () => {
      return new Promise(resolve => {
        let flag = true
        let SEC = TELLER_TIMEOUT_CONFIG.SIGNOUT_COUNTDOWN_MIN * 60

        log(`${TELLER_TIMEOUT_CONFIG.MOVE_SEC / 60}分钟未操作签退提示`)
        MessageBox.alert(
          `检测到已经有${TELLER_TIMEOUT_CONFIG.MOVE_SEC /
            60}分钟没有操作，系统将在<span id="signout-sec">${SEC}</span>秒内自动签退`,
          '自动签退提示',
          {
            confirmButtonText: '取消',
            dangerouslyUseHTMLString: true,
            showClose: false,
            callback: action => {
              if (action === 'confirm') {
                CommonRepo.sessionValid()
                flag = false
                resolve(false)
              }
            }
          }
        )

        let secTime = setInterval(() => {
          SEC--
          try {
            const element = document.getElementById('signout-sec')
            if (element) element.innerHTML = SEC
          } catch (e) {}
          if (SEC <= 0 || !flag) {
            clearInterval(secTime)
            secTime = null
            MessageBox.close()
            resolve(true)
          }
        }, 1000)
      })
    }

    // 主循环
    return new Promise(resolve => {
      console.log('\n[cssp-listener-teller-working]监听柜员超时未操作自动签退日志：开启')
      log('监听柜员超时未操作自动签退日志：开启')
      listenerMouse()

      const timer = setInterval(async () => {
        // ctie 页面会话保活
        if (window.location.hash.includes('ctie')) {
          setLastTime(true)
        }

        // 超时检测
        if (checkIsTimeout() && isLogin()) {
          clearInterval(timer)
          const isSignout = await showTimeoutConfirm()
          removeListenerMouse()
          resolve(isSignout)
          console.log('\n[cssp-listener-teller-working]监听柜员超时未操作自动签退日志：关闭')
          log(
            `监听柜员超时未操作自动签退日志：关闭。触发条件：${
              isSignout ? '超时自动签退' : '手动取消签退'
            }`
          )
        }
      }, 1000)
    })
  }
}