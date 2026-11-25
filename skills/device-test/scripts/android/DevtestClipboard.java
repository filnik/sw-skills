import android.content.ClipData;
import android.os.IBinder;
import java.lang.reflect.Method;
import java.util.Base64;

/** Shell clipboard bridge for native KEYCODE_PASTE, without installing an IME or changing the app. */
public class DevtestClipboard {
    public static void main(String[] args) throws Exception {
        IBinder binder = (IBinder) Class.forName("android.os.ServiceManager")
            .getMethod("getService", String.class).invoke(null, "clipboard");
        Class<?> api = Class.forName("android.content.IClipboard");
        Object clipboard = Class.forName("android.content.IClipboard$Stub")
            .getMethod("asInterface", IBinder.class).invoke(null, binder);
        String text = new String(Base64.getDecoder().decode(args[0]), "UTF-8");
        for (Method method : api.getMethods()) {
            if (!method.getName().equals("setPrimaryClip")) continue;
            Class<?>[] types = method.getParameterTypes();
            Object[] values = new Object[types.length];
            values[0] = ClipData.newPlainText("devtest", text);
            values[1] = "com.android.shell";
            // Android adds attribution/device parameters to the Binder signature across releases.
            for (int i = 2; i < types.length; i++) values[i] = types[i] == int.class ? 0 : null;
            method.invoke(clipboard, values);
            System.out.println("clipboard set: " + text.length() + " characters");
            return;
        }
        throw new IllegalStateException("setPrimaryClip unavailable");
    }
}
