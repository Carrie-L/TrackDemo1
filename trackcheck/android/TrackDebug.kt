// ============================================================================
// 接入 trackcheck 的 Android 端代码（只在 debug 包生效）
// 1) 埋点日志：建议统一打印成一行 JSON（比 data class toString 更稳：值里有逗号/括号也不会解析错）
// 2) 接口 dump：OkHttp 拦截器把每个接口的最新响应写到 /sdcard/Android/data/<包名>/files/api_dump/
//    工具用 `python trackcheck.py pull` 拉下来，作为"接口下发值"的对照数据源
// ============================================================================
package com.example.app.debug

import android.content.Context
import android.util.Log
import com.google.gson.Gson
import okhttp3.Interceptor
import okhttp3.Response
import java.io.File

object TrackDebugLogger {
    private const val TAG = "Tracker"
    private val gson = Gson()

    /** 在埋点 SDK 的统一上报入口调用：TrackDebugLogger.log(eventId, event) */
    fun log(eventId: Long, event: Any) {
        if (!BuildConfig.DEBUG) return
        // 现有格式 "Event: id: xxx data: XxxEvent(a=1, b=2)" 工具也能解析；改成 JSON 更稳
        val line = "Event: id: $eventId data: ${gson.toJson(event)}"
        // 注意 logcat 单行上限约 4000 字节，超长会被截断（埋点一般不会超；超了就精简 debug 打印的内容）
        Log.d(TAG, line)
    }
}

class ApiDumpInterceptor(private val context: Context) : Interceptor {
    override fun intercept(chain: Interceptor.Chain): Response {
        val request = chain.request()
        val response = chain.proceed(request)
        if (!BuildConfig.DEBUG) return response
        runCatching {
            val body = response.peekBody(10L * 1024 * 1024).string() // 不消费原始 body
            if (body.trimStart().startsWith("{") || body.trimStart().startsWith("[")) {
                // /v1/mine/page -> v1_mine_page.json（同一接口只保留最新一次，便于和埋点对照）
                val name = request.url.encodedPath.trim('/').replace('/', '_').ifEmpty { "root" }
                val dir = File(context.getExternalFilesDir(null), "api_dump").apply { mkdirs() }
                File(dir, "$name.json").writeText(body)
            }
        }.onFailure { Log.w("ApiDump", "dump failed: ${request.url}", it) }
        return response
    }
}

// 在 debug 的 OkHttpClient 构建处：
// if (BuildConfig.DEBUG) builder.addInterceptor(ApiDumpInterceptor(appContext))
