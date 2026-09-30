using System;

namespace KspAutomationBridge
{
    /// <summary>
    /// The EVA walker's control math, kept free of Unity and KSP types so it is tested offline
    /// (Tests/BridgeTestHost.cs). Vectors are double[3] in any one consistent world frame.
    ///
    /// Stock facts (KSP 1.12.5): KerbalEVA.UpdatePackLinear applies packTgtRPos x thrustPercentage/100 x
    /// linPower (kN) as a force; StartEVA multiplies linPower by massMultiplier (kerbalEVA.cfg: 8.63 x 0.03
    /// = 0.26 kN). On a ~0.1 t kerbal that is ~2.6 m/s^2: the pack lifts a kerbal on the Mun or Minmus but
    /// not on Kerbin, Eve, Laythe, Tylo, and barely on Duna or Moho.
    /// </summary>
    internal static class EvaMath
    {
        /// <summary>Within this horizontal distance of the target the hop ends: the pack is stowed and the kerbal drops.</summary>
        public const double HopReleaseM = 0.6;

        /// <summary>Horizontal speed limit of a hop (m/s).</summary>
        public const double HopMaxSpeed = 1.5;

        /// <summary>Full-thrust jetpack acceleration must exceed local gravity by this factor to climb and still steer.</summary>
        public const double LiftMargin = 1.3;

        /// <summary>
        /// Between two physics ticks of a simulated kerbal UT moves by one fixed step (0.02 s, 0.08 s at 4x
        /// physics warp). A larger jump means the kerbal was packed or on rails in between.
        /// </summary>
        public const double RailsGapS = 1.0;

        /// <summary>The jetpack's full-thrust acceleration (m/s^2) from linPower (kN), thrustPercentage (0..100) and mass (t).</summary>
        public static double JetpackAccel(double linPower, double thrustPercentage, double massT)
        {
            return linPower * thrustPercentage * 0.01 / Math.Max(0.01, massT);
        }

        /// <summary>Whether a pack with this full-thrust acceleration can lift the kerbal against gravity (both m/s^2).</summary>
        public static bool CanLift(double jetpackAccel, double gravity)
        {
            return jetpackAccel >= LiftMargin * gravity;
        }

        /// <summary>A kerbal this high above the terrain can glide clear even when the pack cannot lift it.</summary>
        public const double GlideMinHeightM = 1.0;

        /// <summary>Below this fraction of gravity the pack cannot even slow a fall enough to glide.</summary>
        public const double GlideMinFraction = 0.5;

        /// <summary>
        /// Whether a hop can fly: either the pack lifts the kerbal, or the kerbal starts at least
        /// GlideMinHeightM above the terrain (at a hatch, on a tank) and the pack gives at least
        /// GlideMinFraction of gravity, so it glides outward while sinking slowly (Duna: 2.8 of 2.9 m/s^2).
        /// </summary>
        public static bool CanHop(double jetpackAccel, double gravity, double heightAboveTerrain)
        {
            if (CanLift(jetpackAccel, gravity))
            {
                return true;
            }
            return heightAboveTerrain >= GlideMinHeightM && jetpackAccel >= GlideMinFraction * gravity;
        }

        /// <summary>
        /// The jetpack request for one physics tick: a thrust fraction vector of magnitude at most 1.
        /// A velocity controller with gravity compensation: toward the target at up to HopMaxSpeed
        /// (slowing within the last metres) while climbing or sinking toward the cruise height.
        /// </summary>
        /// <param name="horizontal">Horizontal offset from the kerbal to the target (m).</param>
        /// <param name="up">Unit local vertical at the kerbal.</param>
        /// <param name="heightError">Cruise altitude minus current altitude (m).</param>
        /// <param name="surfaceVelocity">The kerbal's velocity relative to the surface (m/s).</param>
        /// <param name="gravity">Gravitational acceleration at the kerbal (m/s^2, pointing down).</param>
        /// <param name="jetpackAccel">Full-thrust acceleration (JetpackAccel).</param>
        public static double[] HopCommand(double[] horizontal, double[] up, double heightError, double[] surfaceVelocity,
            double[] gravity, double jetpackAccel)
        {
            double distance = Norm(horizontal);
            double speed = Math.Min(HopMaxSpeed, 0.8 * distance);
            double climb = Math.Max(-1.0, Math.Min(1.0, heightError));
            double scale = Math.Max(0.1, jetpackAccel);
            double[] cmd = new double[3];
            for (int i = 0; i < 3; i++)
            {
                double wanted = (distance > 1e-9 ? horizontal[i] / distance * speed : 0.0) + up[i] * climb;
                cmd[i] = ((wanted - surfaceVelocity[i]) * 1.5 - gravity[i]) / scale;
            }
            double magnitude = Norm(cmd);
            if (magnitude > 1.0)
            {
                for (int i = 0; i < 3; i++)
                {
                    cmd[i] /= magnitude;
                }
            }
            return cmd;
        }

        /// <summary>
        /// Game time (s) between two ticks of the walker that the kerbal was not simulated (packed or on
        /// rails), or 0 for a normal physics step or the first tick. Stall and hop timers skip it.
        /// </summary>
        public static double RailsGap(double lastUt, double ut)
        {
            if (double.IsNaN(lastUt) || double.IsNaN(ut))
            {
                return 0.0;
            }
            double gap = ut - lastUt;
            return gap > RailsGapS ? gap : 0.0;
        }

        public static double Norm(double[] v)
        {
            return Math.Sqrt(v[0] * v[0] + v[1] * v[1] + v[2] * v[2]);
        }
    }
}
